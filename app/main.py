"""FastAPI app: chat + confirm endpoints and the single-page UI.

The cal.com tools run in a separate MCP server process (calcom_mcp.server) that
this app talks to over stdio — the same server Claude Desktop can use.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import BaseModel, Field

from app.agent import Agent
from app.config import load_settings
from app.llm import LLM
from app.sessions import DynamoStore, MemoryStore
from calcom_mcp import timeutil as tu

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    if not settings.cal_api_key:
        raise RuntimeError("CAL_API_KEY is not set — copy .env.example to .env and fill it in.")
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "calcom_mcp.server"],
        # PYTHONPATH carries the Lambda layer's packages; the MCP client doesn't inherit it by default.
        env={
            "CAL_API_KEY": settings.cal_api_key,
            "CAL_API_BASE": settings.cal_api_base,
            **({"PYTHONPATH": os.environ["PYTHONPATH"]} if "PYTHONPATH" in os.environ else {}),
        },
        cwd=ROOT,
    )
    async with AsyncExitStack() as stack:
        mcp = await stack.enter_async_context(Client(server))
        agent = Agent(LLM(settings), mcp)
        await agent.start()
        app.state.agent = agent
        app.state.settings = settings
        app.state.sessions = DynamoStore(settings.sessions_table) if settings.sessions_table else MemoryStore()
        log.info("ready: provider=%s model=%s", settings.llm_provider, settings.model)
        yield


app = FastAPI(title="LiveX.AI Assistant", lifespan=lifespan)


@app.middleware("http")
async def passcode_gate(request: Request, call_next):
    """The hosted URL can change your calendar, so the API needs the passcode (sent by the UI as a header)."""
    expected = request.app.state.settings.access_passcode
    if expected and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        given = unquote(request.headers.get("x-passcode", ""))
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return JSONResponse({"detail": "passcode"}, status_code=401)
    return await call_next(request)


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    timezone: str = "UTC"


class ConfirmIn(BaseModel):
    session_id: str
    action_id: str
    approve: bool


async def _turn(session_id: str, run):
    store = app.state.sessions
    session = await store.get(session_id)
    try:
        return (await _guard(run(session))).to_dict()
    finally:
        await store.put(session)


async def _guard(coro):
    try:
        return await coro
    except anthropic.AuthenticationError:
        raise HTTPException(502, "The language model rejected our credentials — check the API key or AWS profile in .env.")
    except anthropic.APIStatusError as e:
        log.warning("LLM error %s: %s", e.status_code, e.message)
        detail = (e.body or {}).get("error", {}).get("message") if isinstance(e.body, dict) else None
        if e.status_code < 500 and detail:
            raise HTTPException(502, f"The language model said: {detail}")
        raise HTTPException(502, f"The language model returned an error ({e.status_code}). Please try again.")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Couldn't reach the language model. Check your network and API key.")


@app.post("/api/chat")
async def chat(body: ChatIn):
    return await _turn(body.session_id, lambda s: app.state.agent.chat(s, body.message.strip(), body.timezone))


@app.post("/api/confirm")
async def confirm(body: ConfirmIn):
    return await _turn(body.session_id, lambda s: app.state.agent.confirm(s, body.action_id, body.approve))


@app.post("/api/reset")
async def reset(body: dict):
    await app.state.sessions.delete(str(body.get("session_id", "")))
    return {"ok": True}


@app.get("/api/agenda")
async def agenda(timezone: str = "UTC", days: int = 14, start: str | None = None, end: str | None = None):
    """Meetings for the sidebar (next `days`) or the calendar view (`start`..`end`, local dates). No LLM involved."""
    try:
        first = tu.parse_date(start) if start else datetime.now(tu.zone(timezone)).date()
        last = tu.parse_date(end) if end else first + timedelta(days=max(1, min(days, 31)) - 1)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not first <= last <= first + timedelta(days=62):
        raise HTTPException(400, "Pick a date range of at most two months.")
    mcp = app.state.agent.mcp
    if getattr(app.state, "profile", None) is None:
        me = await mcp.call_tool("get_profile", {})
        app.state.profile = me.structured_content if not me.is_error else {}
    result = await mcp.call_tool(
        "list_bookings", {"timezone": timezone, "date_from": first.isoformat(), "date_to": last.isoformat()}
    )
    if result.is_error:
        raise HTTPException(502, "Couldn't load your calendar from cal.com.")
    return {"profile": app.state.profile, **result.structured_content}


@app.get("/api/health")
async def health():
    s = app.state.settings
    return {"ok": True, "provider": s.llm_provider, "model": s.model, "tools": [t["name"] for t in app.state.agent.tools]}


# Local dev serves the UI too; on AWS, CloudFront serves it from S3. Mounted last so /api/* wins.
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
