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
YES_RE = re.compile(
    r"^\s*(y|yes|yep|yeah|yup|sure|ok|okay|confirm(ed)?|do it|go ahead|book it|sounds good|perfect|please do)\b[\s.!]*$",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """\
You are the scheduling assistant for a busy founder who runs their day out of cal.com. They type short, casual \
requests; you handle them like a sharp chief of staff: fast, exact, no fuss.

How you work:
- Use tools for every fact. Never invent times, availability, event types, or booking uids.
- Results from earlier messages in the conversation may be stale (the user can change things in cal.com any time): \
fetch again for a new request, and before saying something isn't possible. Within one request, use what you just \
fetched; never repeat a call with the same arguments.
- Ask only for what you truly need, all in one message. A new booking needs the attendee's name and email plus a time; \
the length defaults to the event type's, so don't ask about it unless the user brought it up. \
Pick the event type yourself when the request makes it clear (e.g. "30-min intro"); ask only if it's ambiguous.
- For a length no fixed event type has (e.g. 90 min), use an event type whose duration_options include it, \
including hidden ones, and pass duration_minutes. If none offers it, say which lengths are available.
- Resolve relative dates ("tomorrow", "Thursday afternoon", "later today") with the date table in the latest <context> \
note. Use part_of_day for morning/afternoon/evening, and after_time for "later today".
- Offer times only once you know the day and the meeting length. If no specific day was given ("book a call with \
Sam", "sometime this week"), don't search or offer times yet: ask which day, with the next few workdays as replies \
(e.g. "Tomorrow", "Thu Oct 22"). If the length is unclear ("a quick call") and event types offer more than one fitting \
length, ask for it too, with the lengths as replies ("15 min", "30 min"). A stated length ("30-min call") or a single \
fitting event type counts as known. Ask for everything missing in one message.
- Once the day and length are known, search that day (or the part of day given) in one find_available_slots call \
and suggest 2–4 times; the app shows every open time on that day.
- When the user refers to an existing meeting ("my 3pm", "the intro with Jordan"), call list_bookings for that day. \
If more than one matches, ask which one.
- Name every new meeting (create_booking `title`): use the name the user gave, otherwise a short one from the \
conversation, like "Intro call with Andy" or "Investor sync". Don't ask for a name.
- Changes (create, reschedule, cancel): once you have everything, call the tool directly. The app shows a confirmation \
card and only runs the action after the user clicks Confirm, so never ask "shall I book it?".
- If a tool result says the user declined or didn't confirm, acknowledge briefly and don't retry unless asked.
- Rescheduling gives the booking a NEW uid. Use the new uid for any follow-up.

How you answer: your final reply is JSON that the app renders.
- message: the answer in one or two short sentences, in plain words. Lead with the answer. No greetings, no "Good \
news", no apologies, no commentary on earlier replies or on what you checked, no restating the request. Never list \
times or meetings in the message; put them in `times` and `meetings`, which the app shows as a time picker and cards.
- times: once the day and length are settled, 2–4 slot `start` values (exactly as find_available_slots returned \
them) you'd suggest on that day. The app also shows every other open time that day. Empty otherwise.
- meetings: uids of the bookings the user asked about, in time order. Empty otherwise.
- replies: up to 5 short follow-ups the user is likely to tap next, e.g. "Another day", "Make it 30 min", or the days \
and lengths you're asking about ("Tomorrow", "Thu Oct 22", "Fri Oct 23", "15 min", "30 min"). Don't repeat the \
offered times. Empty if nothing fits.

Examples:
- "anything tomorrow?" -> message "Two meetings tomorrow, first at 10:00 AM.", meetings [uid1, uid2].
- "find 90 min with Jordan thursday afternoon" -> message "Three 90-minute openings Thursday afternoon. Which works, \
and what's Jordan's email?", times [three starts], replies ["Another day"].
- Clear calendar -> message "Nothing on Thursday.", replies ["Book something Thursday"].
"""

# Structured outputs: the model's final text is always this JSON, which the app renders.
REPLY_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "times": {"type": "array", "items": {"type": "string"}},
            "meetings": {"type": "array", "items": {"type": "string"}},
            "replies": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["message", "times", "meetings", "replies"],
        "additionalProperties": False,
    },
}


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
    slots: set[str] = field(default_factory=set)  # every slot start cal.com returned; the only times we show
    offered: list[str] = field(default_factory=list)  # all open starts found while answering the current request
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Turn:
    reply: str
    times: list[dict[str, Any]] = field(default_factory=list)  # {start, day, time, suggested} for the time picker
    meetings: list[dict[str, Any]] = field(default_factory=list)  # compact bookings for meeting cards
    suggestions: list[str] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    calendar_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "times": self.times,
            "meetings": self.meetings,
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
        seen: set[str] = set()  # read-only calls made for this request
        session.offered = []
        for _ in range(MAX_ROUNDS):
            response = await self.llm.create(system=system, tools=self.tools, messages=session.messages)
            # Append the full content (thinking, text, tool_use blocks) unchanged — history stays append-only.
            session.messages.append({"role": "assistant", "content": response.content})
            text = "".join(b.text for b in response.content if b.type == "text").strip()

            if response.stop_reason == "refusal":
                return Turn(reply="Sorry — I can't help with that one. Anything on your calendar I can do instead?")
            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    return Turn(reply="That answer ran long and got cut off. Could you ask for a bit less at once?")
                return self._final(session, text)

            calls = [b for b in response.content if b.type == "tool_use"]
            gated = [c for c in calls if c.name in self.gated]
            ready = await asyncio.gather(*(self._read(session, c, seen) for c in calls if c.name not in self.gated))
            if gated:
                pending = PendingAction(
                    id=uuid.uuid4().hex[:12],
                    calls=gated,
                    ready_results=list(ready),
                    items=[self._describe(session, c) for c in gated],
                )
                session.pending = pending
                turn = self._final(session, text)
                turn.pending_action = {"id": pending.id, "items": pending.items}
                return turn
            session.messages.append({"role": "user", "content": list(ready)})

        # Out of rounds: answer from what was gathered (tools off) rather than give up.
        response = await self.llm.create(
            system=system, tools=self.tools, messages=session.messages, tool_choice={"type": "none"}
        )
        session.messages.append({"role": "assistant", "content": response.content})
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason != "end_turn" or not text:
            return Turn(reply="I couldn't finish that one. Could you narrow it down, e.g. one day or one person?")
        return self._final(session, text)

    def _final(self, session: Session, text: str) -> Turn:
        """Turn the model's JSON reply into what the UI renders, keeping only times and meetings cal.com returned."""
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return Turn(reply=text)  # plain text, e.g. a short line before a confirmation card
        tz = tu.zone(session.timezone)
        picks = []
        for raw in data.get("times") or []:
            try:
                start = tu.to_utc_z(tu.parse_iso(raw))
            except (TypeError, ValueError):
                continue
            if start in session.slots and start not in picks:
                picks.append(start)
        # When the suggestions are on one day, show every open time that day with them highlighted;
        # across several days the day isn't settled yet, so show just the suggestions.
        day_of = lambda s: tu.parse_iso(s).astimezone(tz).date()
        days = {day_of(p) for p in picks}
        shown = sorted(set(picks) | {s for s in session.offered if day_of(s) in days}) if len(days) == 1 else sorted(picks)
        times = []
        for start in shown:
            local = tu.parse_iso(start).astimezone(tz)
            times.append({"start": start, "day": tu.fmt_day(local), "time": tu.fmt_time(local), "suggested": start in picks})
        meetings = [session.bookings[uid] for uid in data.get("meetings") or [] if uid in session.bookings]
        replies = [r.strip() for r in data.get("replies") or [] if r.strip()][:5]
        return Turn(reply=(data.get("message") or "").strip(), times=times, meetings=meetings, suggestions=replies)

    async def _read(self, session: Session, call: Any, seen: set[str]) -> dict[str, Any]:
        """Run a read-only tool, but don't let the model re-run an identical lookup in a loop."""
        key = call.name + json.dumps(call.input, sort_keys=True)
        if key in seen:
            return {"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                    "content": "You already made this exact call for this request; use that result and answer."}
        seen.add(key)
        return await self._execute(session, call)

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
        """Cache bookings, event types and offered slots, for confirmation cards and to validate the reply."""
        for b in data.get("bookings") or []:
            session.bookings[b["uid"]] = b
        for key in ("booked", "rescheduled"):
            if isinstance(data.get(key), dict):
                session.bookings[data[key]["uid"]] = data[key]
        for et in data.get("event_types") or []:
            session.event_types[et["id"]] = et
        if "days" in data:  # a slot search
            found = {slot["start"] for day in data["days"] for slot in day.get("slots") or []}
            session.slots.update(found)
            session.offered = sorted(set(session.offered) | found)

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
            guests = len(args.get("guests") or [])
            item = {
                "action": "Book",
                "title": args.get("title") or et.get("title") or "New meeting",
                "when": when(args.get("start")),
                "detail": f"{args.get('attendee_name')} · {args.get('attendee_email')}"
                + (f" · +{guests} guest{'s' * (guests > 1)}" if guests else ""),
            }
            if args.get("location"):
                item["where"] = args.get("location_detail") or args["location"].replace("-", " ").title()
            if args.get("allow_conflicts"):
                item["warning"] = "Overlaps another meeting on your calendar"
            return item
        booking = session.bookings.get(args.get("booking_uid"), {})
        title = booking.get("title") or f"Booking {args.get('booking_uid')}"
        if call.name == "reschedule_booking":
            return {"action": "Reschedule", "title": title, "when": when(args.get("new_start")), "detail": f"from {booking.get('when', 'its current time')}"}
        if call.name == "cancel_booking":
            return {"action": "Cancel", "title": title, "when": booking.get("when", ""), "detail": args.get("reason") or ""}
        return {"action": call.name, "title": "", "when": "", "detail": json.dumps(args)}
