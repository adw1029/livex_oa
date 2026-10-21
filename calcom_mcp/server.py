"""cal.com MCP server.

Exposes the founder's calendar as MCP tools. Guards live here, not in the
agent, so every MCP client (this app, Claude Desktop, Claude Code) gets the same
safety checks: slots are re-verified before booking, booking UIDs must exist and
be active, and times are converted in code rather than by the model.

Run standalone:  python -m calcom_mcp.server   (stdio transport)
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from calcom_mcp import timeutil as tu
from calcom_mcp.client import CalClient, CalComError

MAX_SLOTS_PER_DAY = 12
MAX_DAYS = 7
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TzArg = Annotated[str, Field(description="User's IANA timezone, e.g. 'America/Los_Angeles'.")]
DateArg = Annotated[str, Field(description="Local date, YYYY-MM-DD.")]


def compact_booking(b: dict[str, Any], tz: Any) -> dict[str, Any]:
    start, end = tu.parse_iso(b["start"]), tu.parse_iso(b["end"])
    event_type = b.get("eventType") or {}
    return {
        "uid": b.get("uid"),
        "title": b.get("title"),
        "status": b.get("status"),
        "when": tu.fmt_range(start, end, tz),
        "start": tu.to_utc_z(start),
        "end": tu.to_utc_z(end),
        "duration_minutes": b.get("duration") or int((end - start).total_seconds() // 60),
        "event_type_id": event_type.get("id") or b.get("eventTypeId"),
        "attendees": [{"name": a.get("name"), "email": a.get("email")} for a in b.get("attendees") or []],
        "location": b.get("location") or b.get("meetingUrl"),
    }


def _slot_starts(slots_by_day: dict[str, list[Any]]) -> list[datetime]:
    starts = []
    for items in slots_by_day.values():
        for item in items:
            raw = item.get("start") if isinstance(item, dict) else item
            if raw:
                starts.append(tu.parse_iso(raw))
    return sorted(starts)


def create_server(cal: Any) -> MCPServer:
    """Build the MCP server around a cal.com client (real `CalClient` or a test fake)."""
    server = MCPServer(
        "calcom",
        instructions=(
            "Manage the user's cal.com calendar. Use find_available_slots before booking or "
            "rescheduling, and pass slot `start` values through unchanged."
        ),
    )
    host: dict[str, Any] = {}

    async def host_email() -> str | None:
        if "email" not in host:
            try:
                host["email"] = (await cal.me()).get("email")
            except CalComError:
                host["email"] = None
        return host["email"]

    async def active_booking(uid: str) -> dict[str, Any]:
        found = await cal.list_bookings(booking_uid=uid)
        booking = next((b for b in found if b.get("uid") == uid), None)
        if not booking:
            raise ToolError(f"No booking with uid {uid!r}. Call list_bookings to find the right one.")
        if booking.get("status") in ("cancelled", "rejected"):
            raise ToolError(f"Booking {uid!r} is already {booking['status']}.")
        return booking

    async def slot_is_open(event_type_id: int, start: datetime, reschedule_uid: str | None) -> bool:
        # cal.com only returns slots that fit entirely inside the window, so give it room for the meeting.
        slots = await cal.get_slots(
            event_type_id=event_type_id,
            start=tu.to_utc_z(start - timedelta(minutes=1)),
            end=tu.to_utc_z(start + timedelta(days=1)),
            time_zone="UTC",
            booking_uid_to_reschedule=reschedule_uid,
        )
        return start in _slot_starts(slots)

    def parse_start(value: str) -> datetime:
        try:
            start = tu.parse_iso(value)
        except ValueError:
            raise ToolError(f"Invalid start {value!r}; pass a `start` value from find_available_slots.")
        if start <= datetime.now(UTC):
            raise ToolError("That time is in the past.")
        return start

    async def call_cal(coro):
        try:
            return await coro
        except CalComError as e:
            raise ToolError(f"cal.com rejected the request: {e.message}")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
    async def get_profile() -> dict[str, Any]:
        """The calendar owner's name, email, and default timezone."""
        me = await call_cal(cal.me())
        return {"name": me.get("name"), "email": me.get("email"), "username": me.get("username"), "timezone": me.get("timeZone")}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
    async def list_event_types() -> dict[str, Any]:
        """List the host's bookable event types (id, title, length). Use this to pick event_type_id."""
        event_types = await call_cal(cal.list_event_types())
        return {
            "event_types": [
                {
                    "id": et.get("id"),
                    "title": et.get("title"),
                    "slug": et.get("slug"),
                    "length_minutes": et.get("lengthInMinutes") or et.get("length"),
                    "hidden": bool(et.get("hidden")),
                }
                for et in event_types
            ]
        }

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
    async def find_available_slots(
        event_type_id: int,
        date_from: DateArg,
        timezone: TzArg,
        date_to: Annotated[str | None, Field(description="Last local date to search (inclusive). Defaults to date_from.")] = None,
        part_of_day: Literal["morning", "afternoon", "evening", "any"] = "any",
        after_time: Annotated[str | None, Field(description="Only slots at/after this local time, HH:MM (e.g. for 'later today').")] = None,
        reschedule_booking_uid: Annotated[str | None, Field(description="When rescheduling, the booking being moved (its own slot counts as free).")] = None,
    ) -> dict[str, Any]:
        """Find open times for an event type within a local date range. Returns slots grouped by day,
        each with a `start` (UTC, pass to create/reschedule unchanged) and a human `label` in the user's timezone.
        part_of_day windows: morning 8-12, afternoon 12-17, evening 17-21 (local)."""
        try:
            tz = tu.zone(timezone)
            day_from = tu.parse_date(date_from)
            day_to = tu.parse_date(date_to) if date_to else day_from
        except ValueError as e:
            raise ToolError(str(e))
        if day_to < day_from:
            day_from, day_to = day_to, day_from
        day_to = min(day_to, day_from + timedelta(days=MAX_DAYS - 1))
        start, end = tu.local_day_bounds(day_from, day_to, tz)
        start = max(start, datetime.now(tz).replace(second=0, microsecond=0))
        if start >= end:
            return {"timezone": timezone, "days": [], "note": "That date range is already in the past."}

        slots = await call_cal(
            cal.get_slots(
                event_type_id=event_type_id,
                start=tu.to_utc_z(start),
                end=tu.to_utc_z(end),
                time_zone=timezone,
                booking_uid_to_reschedule=reschedule_booking_uid,
            )
        )
        window = tu.PARTS_OF_DAY.get(part_of_day)
        min_minutes = None
        if after_time:
            try:
                hh, mm = (int(x) for x in after_time.split(":"))
                min_minutes = hh * 60 + mm
            except ValueError:
                raise ToolError("after_time must look like HH:MM.")

        days: dict[str, list[dict[str, str]]] = {}
        for s in _slot_starts(slots):
            local = s.astimezone(tz)
            if window and not (window[0] <= local.hour < window[1]):
                continue
            if min_minutes is not None and local.hour * 60 + local.minute < min_minutes:
                continue
            days.setdefault(local.date().isoformat(), []).append({"start": tu.to_utc_z(s), "label": tu.fmt_local(s, tz)})

        result = [
            {"date": d, "day": tu.fmt_day(tu.parse_date(d)), "slots": items[:MAX_SLOTS_PER_DAY], "total": len(items)}
            for d, items in sorted(days.items())
        ]
        return {"timezone": timezone, "days": result, "note": None if result else "No open slots in that window."}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
    async def list_bookings(
        timezone: TzArg,
        date_from: Annotated[str | None, Field(description="First local date, YYYY-MM-DD.")] = None,
        date_to: Annotated[str | None, Field(description="Last local date (inclusive). Defaults to date_from.")] = None,
        status: Annotated[
            Literal["upcoming", "past", "cancelled", "unconfirmed"] | None,
            Field(description="Omit for all active (non-cancelled) bookings in the date range; defaults to upcoming when no dates are given."),
        ] = None,
        attendee_name: str | None = None,
    ) -> dict[str, Any]:
        """List the user's bookings, sorted by start time, with times in the user's timezone."""
        try:
            tz = tu.zone(timezone)
            after = before = None
            if date_from:
                day_from = tu.parse_date(date_from)
                day_to = tu.parse_date(date_to) if date_to else day_from
                start, end = tu.local_day_bounds(day_from, day_to, tz)
                after, before = tu.to_utc_z(start), tu.to_utc_z(end)
        except ValueError as e:
            raise ToolError(str(e))
        if status is None and not date_from:
            status = "upcoming"
        bookings = await call_cal(
            cal.list_bookings(status=status, after_start=after, before_end=before, attendee_name=attendee_name)
        )
        if status is None:
            bookings = [b for b in bookings if b.get("status") not in ("cancelled", "rejected")]
        items = sorted((compact_booking(b, tz) for b in bookings), key=lambda b: b["start"])
        return {"timezone": timezone, "count": len(items), "bookings": items}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def create_booking(
        event_type_id: int,
        start: Annotated[str, Field(description="Slot `start` from find_available_slots (UTC ISO 8601).")],
        attendee_name: str,
        attendee_email: str,
        attendee_timezone: Annotated[str, Field(description="Attendee's IANA timezone; use the user's if unknown.")],
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Book a meeting with an attendee. The slot is re-checked against live availability first."""
        if not attendee_name.strip():
            raise ToolError("attendee_name is required — ask the user who the meeting is with.")
        if not EMAIL_RE.match(attendee_email.strip()):
            raise ToolError(f"{attendee_email!r} doesn't look like an email — ask the user for it.")
        try:
            tz = tu.zone(attendee_timezone)
        except ValueError as e:
            raise ToolError(str(e))
        begin = parse_start(start)
        if not await call_cal(slot_is_open(event_type_id, begin, None)):
            raise ToolError("That slot is no longer available. Call find_available_slots again and offer new times.")
        booking = await call_cal(
            cal.create_booking(
                event_type_id=event_type_id,
                start=tu.to_utc_z(begin),
                attendee={"name": attendee_name.strip(), "email": attendee_email.strip(), "timeZone": attendee_timezone},
                notes=notes,
            )
        )
        return {"booked": compact_booking(booking, tz)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    async def reschedule_booking(
        booking_uid: str,
        new_start: Annotated[str, Field(description="Slot `start` from find_available_slots (called with reschedule_booking_uid).")],
        timezone: TzArg,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Move an existing booking to a new time. cal.com issues a NEW uid; use it for any follow-up."""
        try:
            tz = tu.zone(timezone)
        except ValueError as e:
            raise ToolError(str(e))
        booking = await call_cal(active_booking(booking_uid))
        begin = parse_start(new_start)
        event_type_id = (booking.get("eventType") or {}).get("id") or booking.get("eventTypeId")
        if event_type_id and not await call_cal(slot_is_open(event_type_id, begin, booking_uid)):
            raise ToolError("That time isn't available. Call find_available_slots with reschedule_booking_uid and offer new times.")
        moved = await call_cal(
            cal.reschedule_booking(booking_uid, start=tu.to_utc_z(begin), rescheduled_by=await host_email(), reason=reason)
        )
        return {"previous_uid": booking_uid, "rescheduled": compact_booking(moved, tz)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
    async def cancel_booking(booking_uid: str, timezone: TzArg, reason: str | None = None) -> dict[str, Any]:
        """Cancel a booking. Attendees are notified by cal.com."""
        try:
            tz = tu.zone(timezone)
        except ValueError as e:
            raise ToolError(str(e))
        booking = await call_cal(active_booking(booking_uid))
        # cal.com requires a reason when the host cancels; don't make the user invent one.
        await call_cal(cal.cancel_booking(booking_uid, reason=reason or "Cancelled by the host."))
        return {"cancelled": {**compact_booking(booking, tz), "status": "cancelled"}}

    return server


def main() -> None:
    load_dotenv()
    api_key = os.environ.get("CAL_API_KEY")
    if not api_key:
        raise SystemExit("CAL_API_KEY is not set")
    create_server(CalClient(api_key, base_url=os.environ.get("CAL_API_BASE", "https://api.cal.com/v2"))).run("stdio")


if __name__ == "__main__":
    main()
