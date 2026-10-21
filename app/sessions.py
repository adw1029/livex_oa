"""Session storage: in memory locally, DynamoDB on Lambda.

Lambda may run several copies of the app, so a "Confirm" click can land on a
different instance than the chat turn that created the pending action. Sessions
are saved after every request and loaded at the start of the next.
"""

from __future__ import annotations

import asyncio
import json
import time
import zlib
from dataclasses import asdict
from typing import Any

from anthropic.types import ToolUseBlock

from app.agent import PendingAction, Session

TTL_SECONDS = 24 * 3600


def _plain(block: Any) -> Any:
    # Same shape the SDK sends when you pass response.content back to the API.
    return block.model_dump(mode="json", exclude_unset=True) if hasattr(block, "model_dump") else block


def dump(session: Session) -> bytes:
    pending = None
    if session.pending:
        pending = {**asdict(session.pending), "calls": [_plain(c) for c in session.pending.calls]}
    data = {
        "id": session.id,
        "timezone": session.timezone,
        "messages": [{"role": m["role"], "content": [_plain(b) for b in m["content"]]} for m in session.messages],
        "pending": pending,
        "bookings": session.bookings,
        "event_types": session.event_types,
        "slots": sorted(session.slots),
        "offered": session.offered,
    }
    # Compressed: tool results (slots, bookings) repeat a lot, and DynamoDB items cap at 400 KB.
    return zlib.compress(json.dumps(data, ensure_ascii=False).encode())


def load(raw: bytes) -> Session:
    data = json.loads(zlib.decompress(raw))
    pending = data["pending"]
    if pending:
        pending = PendingAction(**{**pending, "calls": [ToolUseBlock.model_validate(c) for c in pending["calls"]]})
    return Session(
        id=data["id"],
        timezone=data["timezone"],
        messages=data["messages"],
        pending=pending,
        bookings=data["bookings"],
        event_types={int(k): v for k, v in data["event_types"].items()},  # JSON object keys are strings
        slots=set(data.get("slots") or []),
        offered=data.get("offered") or [],
    )


class MemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def get(self, session_id: str) -> Session:
        return self._sessions.setdefault(session_id, Session(id=session_id))

    async def put(self, session: Session) -> None:
        pass  # the object itself is what's stored

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class DynamoStore:
    def __init__(self, table_name: str) -> None:
        import boto3

        self._table = boto3.resource("dynamodb").Table(table_name)

    async def get(self, session_id: str) -> Session:
        item = (await asyncio.to_thread(self._table.get_item, Key={"id": session_id})).get("Item")
        return load(item["data"].value) if item else Session(id=session_id)

    async def put(self, session: Session) -> None:
        item = {"id": session.id, "data": dump(session), "expires_at": int(time.time()) + TTL_SECONDS}
        await asyncio.to_thread(self._table.put_item, Item=item)

    async def delete(self, session_id: str) -> None:
        await asyncio.to_thread(self._table.delete_item, Key={"id": session_id})
