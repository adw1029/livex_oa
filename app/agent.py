"""Chat agent: Claude tool-use loop over the cal.com MCP server, with a human confirmation gate.

The gate is enforced in code from MCP tool annotations: any tool not marked
`readOnlyHint` is held as a pending action and only executed after the user
explicitly confirms. The model never sees a fake result — the tool_use stays
open until the user decides, then gets the real outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp import Client

from calcom_mcp import timeutil as tu

log = logging.getLogger("agent")

MAX_ROUNDS = 8
CHOICES_RE = re.compile(r"\n*\[\[([^\]]+)\]\]\s*$")
YES_RE = re.compile(
    r"^\s*(y|yes|yep|yeah|yup|sure|ok|okay|confirm(ed)?|do it|go ahead|book it|sounds good|perfect|please do)\b[\s.!]*$",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """\
You are the scheduling assistant for a busy founder who runs their day out of cal.com. They type short, casual \
requests; you handle them like a sharp executive assistant.

How you work:
- Be brief and warm. Plain sentences, light markdown (bold for times, short lists). No filler, no restating the request.
- Use tools for every fact. Never invent times, availability, event types, or booking uids. Only offer times that \
find_available_slots returned, and pass their `start` value through unchanged.
- Ask only for what you truly need, all in one message. A new booking needs the attendee's name and email plus a time. \
Pick the event type yourself when the request makes it clear (e.g. "30-min intro"); ask only if it's ambiguous.
- Resolve relative dates ("tomorrow", "Thursday afternoon", "later today") with the date table in the latest <context> \
note. Use part_of_day for morning/afternoon/evening, and after_time for "later today".
- Offer 2–4 well-spread options, not a dump. If nothing fits, say so and suggest the nearest alternative.
- When the user refers to an existing meeting ("my 3pm", "the intro with Jordan"), call list_bookings for that day. \
If more than one matches, ask which one.
- Changes (create, reschedule, cancel): once you have everything, call the tool directly. The app shows the user a \
confirmation card and only runs the action after they click Confirm — so don't ask "shall I book it?" in text. Say one \
short line before the call, like "Here's the booking — confirm below."
- If a tool result says the user declined or didn't confirm, acknowledge briefly and don't retry unless asked.
- Rescheduling gives the booking a NEW uid. Use the new uid for any follow-up.
- Show times in the user's timezone, with the zone abbreviation on first mention.
- When the user needs to pick between options, end your message with one line in exactly this format: \
[[option 1 | option 2 | option 3]] — short, clickable replies such as [[Thu 2:00 PM | Thu 3:30 PM | Another day]].
"""


@dataclass
class PendingAction:
    id: str
    calls: list[Any]  # tool_use blocks awaiting confirmation
    ready_results: list[dict[str, Any]]  # results for read-only calls from the same turn
    items: list[dict[str, str]]  # human-readable summary for the confirmation card


@dataclass
class Session:
    id: str
    timezone: str = "UTC"
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending: PendingAction | None = None
    bookings: dict[str, dict[str, Any]] = field(default_factory=dict)  # uid -> compact booking
    event_types: dict[int, dict[str, Any]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Turn:
    reply: str
    suggestions: list[str] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    calendar_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "suggestions": self.suggestions,
            "pending_action": self.pending_action,
            "calendar_changed": self.calendar_changed,
        }


class Agent:
    def __init__(self, llm: Any, mcp: Client):
        self.llm = llm
        self.mcp = mcp
        self.tools: list[dict[str, Any]] = []
        self.gated: set[str] = set()
        self.audit: list[dict[str, Any]] = []

    async def start(self) -> None:
        listing = await self.mcp.list_tools()
        self.tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listing.tools
        ]
        # Anything not explicitly read-only changes the calendar and needs a human click.
        self.gated = {t.name for t in listing.tools if not (t.annotations and t.annotations.read_only_hint)}
        log.info("tools=%s gated=%s", [t["name"] for t in self.tools], sorted(self.gated))

    # ---- public entry points -------------------------------------------------

    async def chat(self, session: Session, text: str, timezone: str | None = None) -> Turn:
        async with session.lock:
            if timezone:
                session.timezone = timezone
            content: list[dict[str, Any]] = []
            if session.pending:
                if YES_RE.match(text):
                    return await self._resolve(session, session.pending, approve=True)
                # The user moved on without clicking: close the open tool calls honestly.
                content += session.pending.ready_results + self._decline(
                    session.pending, "Not executed: the user replied instead of confirming."
                )
                session.pending = None
            content += [
                {"type": "text", "text": f"<context>\n{tu.time_context(session.timezone)}\n</context>"},
                {"type": "text", "text": text},
            ]
            session.messages.append({"role": "user", "content": content})
            return await self._run(session)

    async def confirm(self, session: Session, action_id: str, approve: bool) -> Turn:
        async with session.lock:
            pending = session.pending
            if not pending or pending.id != action_id:
                return Turn(reply="That request has expired — nothing was changed. What would you like to do?")
            return await self._resolve(session, pending, approve)

    # ---- internals -----------------------------------------------------------

    async def _resolve(self, session: Session, pending: PendingAction, approve: bool) -> Turn:
        session.pending = None
        if approve:
            results = [await self._execute(session, call, audit=True) for call in pending.calls]
            changed = any(not r.get("is_error") for r in results)
        else:
            results = self._decline(pending, "The user clicked Cancel. Nothing was changed.")
            changed = False
        session.messages.append({"role": "user", "content": pending.ready_results + results})
        turn = await self._run(session)
        turn.calendar_changed = turn.calendar_changed or changed
        return turn

    def _decline(self, pending: PendingAction, reason: str) -> list[dict[str, Any]]:
        return [{"type": "tool_result", "tool_use_id": c.id, "content": reason, "is_error": True} for c in pending.calls]

    async def _run(self, session: Session) -> Turn:
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        for _ in range(MAX_ROUNDS):
            response = await self.llm.create(system=system, tools=self.tools, messages=session.messages)
            # Append the full content (thinking, text, tool_use blocks) unchanged — history stays append-only.
            session.messages.append({"role": "assistant", "content": response.content})
            text = "".join(b.text for b in response.content if b.type == "text").strip()

            if response.stop_reason == "refusal":
                return Turn(reply="Sorry — I can't help with that one. Anything on your calendar I can do instead?")
            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    text += "\n\n_(Response cut short — ask me to continue.)_"
                return self._final(text)

            calls = [b for b in response.content if b.type == "tool_use"]
            gated = [c for c in calls if c.name in self.gated]
            ready = await asyncio.gather(*(self._execute(session, c) for c in calls if c.name not in self.gated))
            if gated:
                pending = PendingAction(
                    id=uuid.uuid4().hex[:12],
                    calls=gated,
                    ready_results=list(ready),
                    items=[self._describe(session, c) for c in gated],
                )
                session.pending = pending
                turn = self._final(text or "Please confirm:")
                turn.pending_action = {"id": pending.id, "items": pending.items}
                return turn
            session.messages.append({"role": "user", "content": list(ready)})

        return Turn(reply="I got stuck working on that — could you rephrase or break it into steps?")

    def _final(self, text: str) -> Turn:
        suggestions: list[str] = []
        match = CHOICES_RE.search(text)
        if match:
            suggestions = [s.strip() for s in match.group(1).split("|") if s.strip()][:5]
            text = text[: match.start()].rstrip()
        return Turn(reply=text, suggestions=suggestions)

    async def _execute(self, session: Session, call: Any, audit: bool = False) -> dict[str, Any]:
        try:
            result = await self.mcp.call_tool(call.name, call.input)
        except Exception as e:  # transport-level failure; surface it to the model as a tool error
            log.exception("tool %s failed", call.name)
            return {"type": "tool_result", "tool_use_id": call.id, "content": f"Tool failed: {e}", "is_error": True}

        data = result.structured_content
        if data is not None:
            self._remember(session, data)
            payload = json.dumps(data, ensure_ascii=False)
        else:
            payload = "\n".join(getattr(c, "text", "") for c in result.content) or "(no output)"
        if audit:
            entry = {"session": session.id, "tool": call.name, "input": call.input, "ok": not result.is_error}
            self.audit.append(entry)
            log.info("write %s", entry)
        return {"type": "tool_result", "tool_use_id": call.id, "content": payload, "is_error": bool(result.is_error)}

    def _remember(self, session: Session, data: dict[str, Any]) -> None:
        """Cache bookings and event types so confirmation cards can say what's being changed."""
        for b in data.get("bookings") or []:
            session.bookings[b["uid"]] = b
        for key in ("booked", "rescheduled"):
            if isinstance(data.get(key), dict):
                session.bookings[data[key]["uid"]] = data[key]
        for et in data.get("event_types") or []:
            session.event_types[et["id"]] = et

    def _describe(self, session: Session, call: Any) -> dict[str, str]:
        args = call.input or {}
        tz = tu.zone(session.timezone)

        def when(value: str | None) -> str:
            try:
                return f"{tu.fmt_local(tu.parse_iso(value), tz)} {tu.parse_iso(value).astimezone(tz).tzname()}"
            except (TypeError, ValueError):
                return str(value)

        if call.name == "create_booking":
            et = session.event_types.get(args.get("event_type_id"), {})
            return {
                "action": "Book",
                "title": et.get("title") or "New meeting",
                "when": when(args.get("start")),
                "detail": f"{args.get('attendee_name')} · {args.get('attendee_email')}",
            }
        booking = session.bookings.get(args.get("booking_uid"), {})
        title = booking.get("title") or f"Booking {args.get('booking_uid')}"
        if call.name == "reschedule_booking":
            return {"action": "Reschedule", "title": title, "when": when(args.get("new_start")), "detail": f"from {booking.get('when', 'its current time')}"}
        if call.name == "cancel_booking":
            return {"action": "Cancel", "title": title, "when": booking.get("when", ""), "detail": args.get("reason") or ""}
        return {"action": call.name, "title": "", "when": "", "detail": json.dumps(args)}
