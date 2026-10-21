"""In-memory stand-ins for cal.com and the LLM, so tests run without network or keys."""

from __future__ import annotations

import itertools
import json
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from anthropic.types import Message, TextBlock, ToolUseBlock

from calcom_mcp import timeutil as tu
from calcom_mcp.client import CalComError

LA = ZoneInfo("America/Los_Angeles")


def next_weekday(weekday: int, after: date | None = None) -> date:
    """Next given weekday (Mon=0) strictly after `after` (default: today in LA), at least 2 days out."""
    d = (after or datetime.now(LA).date()) + timedelta(days=2)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


class FakeCal:
    def __init__(self, day: date):
        self.day = day
        self.event_types = [
            {"id": 1, "title": "30 Min Intro", "slug": "intro", "lengthInMinutes": 30,
             "locations": [{"type": "integration", "integration": "cal-video"}]},
            {"id": 2, "title": "Office Hours", "slug": "office-hours", "lengthInMinutes": 30, "lengthInMinutesOptions": [15, 30, 60],
             "locations": [{"type": "integration", "integration": "zoom"}, {"type": "address", "address": "1 Market St"},
                           {"type": "attendeeDefined"}]},
        ]
        self.bookings: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._uids = (f"bk{i}" for i in itertools.count(1))
        # Open every 30 minutes, 9:00–17:00 LA time on `day`.
        self.open: set[datetime] = {
            datetime.combine(day, time(9), tzinfo=LA) + timedelta(minutes=30 * i) for i in range(16)
        }

    def at(self, hh: int, mm: int = 0) -> datetime:
        return datetime.combine(self.day, time(hh, mm), tzinfo=LA)

    def seed(self, title: str, start: datetime, attendee: str = "Sam Rivera") -> str:
        uid = next(self._uids)
        self.open.discard(start)
        self.bookings[uid] = self._booking(uid, title, start, attendee)
        return uid

    def _booking(self, uid: str, title: str, start: datetime, attendee: str, status: str = "accepted",
                 event_type: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "uid": uid,
            "title": title,
            "status": status,
            "start": tu.to_utc_z(start),
            "end": tu.to_utc_z(start + timedelta(minutes=30)),
            "duration": 30,
            "eventType": event_type or {"id": 1, "slug": "intro"},
            "attendees": [{"name": attendee, "email": f"{attendee.split()[0].lower()}@example.com"}],
            "location": "https://cal.com/video/x",
        }

    async def me(self) -> dict[str, Any]:
        return {"email": "founder@example.com", "timeZone": "America/Los_Angeles"}

    async def list_event_types(self) -> list[dict[str, Any]]:
        return self.event_types

    async def create_event_type(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create_event_type", body))
        et = {**body, "id": 100 + len(self.event_types)}
        self.event_types.append(et)
        return et

    async def delete_event_type(self, event_type_id: int) -> None:
        self.calls.append(("delete_event_type", {"id": event_type_id}))
        self.event_types = [et for et in self.event_types if et["id"] != event_type_id]

    async def get_slots(self, *, event_type_id, start, end, time_zone, duration=None, booking_uid_to_reschedule=None):
        self.calls.append(("get_slots", {"start": start, "end": end, "duration": duration, "reschedule": booking_uid_to_reschedule}))
        length = timedelta(minutes=duration or 30)
        lo, hi, tz = tu.parse_iso(start), tu.parse_iso(end), ZoneInfo(time_zone)
        open_ = set(self.open)
        if booking_uid_to_reschedule in self.bookings:
            open_.add(tu.parse_iso(self.bookings[booking_uid_to_reschedule]["start"]))
        out: dict[str, list[dict[str, str]]] = {}
        # Like cal.com: only slots that fit entirely inside [start, end] are returned.
        for s in sorted(x for x in open_ if lo <= x and x + length <= hi):
            local = s.astimezone(tz)
            out.setdefault(local.date().isoformat(), []).append(
                {"start": local.isoformat(timespec="milliseconds"), "end": (local + length).isoformat()}
            )
        return out

    async def list_bookings(self, *, status=None, after_start=None, before_end=None, attendee_name=None,
                            attendee_email=None, event_type_id=None, booking_uid=None):
        self.calls.append(("list_bookings", {"status": status, "attendee_email": attendee_email, "event_type_id": event_type_id}))
        items = list(self.bookings.values())
        if attendee_email:
            items = [b for b in items if any(a["email"] == attendee_email for a in b["attendees"])]
        if event_type_id:
            items = [b for b in items if b["eventType"]["id"] == event_type_id]
        if booking_uid:
            items = [b for b in items if b["uid"] == booking_uid]
        if after_start:
            items = [b for b in items if tu.parse_iso(b["start"]) >= tu.parse_iso(after_start)]
        if before_end:
            items = [b for b in items if tu.parse_iso(b["end"]) <= tu.parse_iso(before_end)]
        if status == "upcoming":
            items = [b for b in items if b["status"] == "accepted"]
        elif status == "cancelled":
            items = [b for b in items if b["status"] == "cancelled"]
        return items

    async def create_booking(self, *, event_type_id, start, attendee, length_in_minutes=None, location=None,
                             guests=None, notes=None, allow_conflicts=False):
        self.calls.append(("create_booking", {"start": start, "attendee": attendee, "length_in_minutes": length_in_minutes,
                                              "location": location, "guests": guests, "allow_conflicts": allow_conflicts}))
        begin = tu.parse_iso(start)
        if begin not in self.open and not allow_conflicts:
            raise CalComError(400, "User either already has booking at this time or is not available")
        self.open.discard(begin)
        uid = next(self._uids)
        et = next(e for e in self.event_types if e["id"] == event_type_id)
        # Like cal.com: the title comes from the event type's name template.
        title = et["title"] if et.get("customName") == "{Event type title}" else f"{et['title']} between Founder and {attendee['name']}"
        self.bookings[uid] = self._booking(uid, title, begin, attendee["name"], event_type={"id": et["id"], "slug": et.get("slug")})
        return self.bookings[uid]

    async def reschedule_booking(self, uid, *, start, rescheduled_by=None, reason=None):
        self.calls.append(("reschedule_booking", {"uid": uid, "start": start, "rescheduled_by": rescheduled_by}))
        old = self.bookings[uid]
        old["status"] = "cancelled"
        new_uid = next(self._uids)
        old["rescheduledToUid"] = new_uid
        self.open.add(tu.parse_iso(old["start"]))
        begin = tu.parse_iso(start)
        self.open.discard(begin)
        self.bookings[new_uid] = {**self._booking(new_uid, old["title"], begin, old["attendees"][0]["name"], event_type=old["eventType"]),
                                  "rescheduledFromUid": uid}
        return self.bookings[new_uid]

    async def cancel_booking(self, uid, *, reason=None):
        self.calls.append(("cancel_booking", {"uid": uid, "reason": reason}))
        self.bookings[uid]["status"] = "cancelled"
        self.open.add(tu.parse_iso(self.bookings[uid]["start"]))
        return self.bookings[uid]


# ---- scripted LLM --------------------------------------------------------------

def reply(text: str) -> Message:
    return Message.model_construct(
        id="msg", type="message", role="assistant", model="fake",
        content=[TextBlock.model_construct(type="text", text=text)],
        stop_reason="end_turn",
    )


def answer(message: str, times=(), meetings=(), replies=()) -> Message:
    """A final reply in the app's structured JSON format."""
    return reply(json.dumps({"message": message, "times": list(times), "meetings": list(meetings), "replies": list(replies)}))


def call(name: str, args: dict[str, Any], text: str = "", call_id: str | None = None) -> Message:
    blocks: list[Any] = [TextBlock.model_construct(type="text", text=text)] if text else []
    blocks.append(ToolUseBlock.model_construct(type="tool_use", id=call_id or f"tu_{name}", name=name, input=args))
    return Message.model_construct(
        id="msg", type="message", role="assistant", model="fake", content=blocks, stop_reason="tool_use"
    )


class ScriptedLLM:
    """Returns queued responses in order and records the messages it was sent."""

    def __init__(self, *responses: Message):
        self.queue = list(responses)
        self.seen: list[list[dict[str, Any]]] = []
        self.tool_choices: list[Any] = []

    def push(self, *responses: Message) -> None:
        self.queue.extend(responses)

    async def create(self, *, system, tools, messages, tool_choice=None):
        self.seen.append(list(messages))
        self.tool_choices.append(tool_choice)
        assert self.queue, "LLM called more times than scripted"
        return self.queue.pop(0)
