"""cal.com MCP server.

Exposes the founder's calendar as MCP tools. Guards live here, not in the
agent, so every MCP client (this app, Claude Desktop, Claude Code) gets the same
safety checks: slots are re-verified before booking, booking UIDs must exist and
be active, and times are converted in code rather than by the model.

Run standalone:  python -m calcom_mcp.server   (stdio transport)
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from calcom_mcp import timeutil as tu
from calcom_mcp.client import CalClient, CalComError

MAX_SLOTS_PER_DAY = 48  # a whole day at 30-minute steps
MAX_DAYS = 7
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# cal.com can't name a single booking: its title comes from the event type. So each named meeting is booked on a
# hidden one-off event type with that title; these are tagged by slug and hidden from list_event_types.
NAMED_SLUG_PREFIX = "lx-meeting-"
# Settings copied from the event type the user picked, so the one-off type books exactly like it.
COPIED_SETTINGS = ("locations", "scheduleId", "minimumBookingNotice", "beforeEventBuffer", "afterEventBuffer", "slotInterval")

# Attendee-supplied location types and the field that carries the attendee's value.
ATTENDEE_LOCATIONS = {"attendeeAddress": "address", "attendeeDefined": "location", "attendeePhone": "phone"}

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
        **{
            k: v
            for k, v in {
                "guests": b.get("guests"),
                "notes": (b.get("bookingFieldsResponses") or {}).get("notes"),
                "cancellation_reason": b.get("cancellationReason"),
                "rescheduled_to_uid": b.get("rescheduledToUid"),
            }.items()
            if v
        },
    }


def location_options(et: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Where an event type can be held: option name (e.g. "zoom", "address", "attendeeDefined") -> its location.

    cal.com only accepts one of the event type's own locations. `address`/`link`/`phone` mean the
    organizer's configured value; the attendee* types carry a value supplied at booking time.
    """
    options: dict[str, dict[str, Any]] = {}
    for loc in et.get("locations") or []:
        key = loc.get("integration") if loc.get("type") == "integration" else loc.get("type")
        if key:
            options[key] = loc
    return options


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
    event_types: dict[int, dict[str, Any]] = {}

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
        if booking.get("rescheduledToUid"):
            raise ToolError(f"Booking {uid!r} was rescheduled; it is now booking {booking['rescheduledToUid']!r}.")
        if booking.get("status") in ("cancelled", "rejected"):
            raise ToolError(f"Booking {uid!r} is already {booking['status']}.")
        return booking

    async def event_type(event_type_id: int) -> dict[str, Any]:
        if event_type_id not in event_types:
            event_types.update({et["id"]: et for et in await cal.list_event_types()})
        if event_type_id not in event_types:
            raise ToolError(f"No event type {event_type_id}. Call list_event_types to pick one.")
        return event_types[event_type_id]

    async def duration_for(event_type_id: int, minutes: int | None) -> int | None:
        """The `duration` to send cal.com: only allowed for event types that offer several lengths."""
        if minutes is None:
            return None
        et = await event_type(event_type_id)
        options = et.get("lengthInMinutesOptions") or []
        if options:
            if minutes not in options:
                raise ToolError(f"{et.get('title')} can be {', '.join(map(str, options))} minutes, not {minutes}.")
            return minutes
        length = et.get("lengthInMinutes") or et.get("length")
        if minutes != length:
            raise ToolError(f"{et.get('title')} is always {length} minutes. Pick an event type with that length.")
        return None  # fixed length: cal.com rejects an explicit duration

    async def booking_location(event_type_id: int, option: str | None, detail: str | None) -> dict[str, Any] | None:
        """The `location` object for a booking, checked against what the event type allows."""
        if not option:
            return None
        et = await event_type(event_type_id)
        options = location_options(et)
        if option not in options:
            allowed = ", ".join(options) or "its default location only"
            raise ToolError(f"{et.get('title')} can be held at: {allowed}. A new kind of place has to be added to the event type in cal.com first.")
        loc = options[option]
        where = {"type": "integration", "integration": option} if loc["type"] == "integration" else {"type": loc["type"]}
        if field := ATTENDEE_LOCATIONS.get(where["type"]):
            if not (detail or "").strip():
                raise ToolError(f"Ask the user for the {field} to use.")
            where[field] = detail.strip()
        return where

    def same_location(et: dict[str, Any], response: Any) -> dict[str, Any] | None:
        """The `location` that reproduces a booking's, from its bookingFieldsResponses.location; None = the default."""
        if not isinstance(response, dict):
            return None
        options = location_options(et)
        detail = (response.get("optionValue") or "").strip()
        if detail:  # a place, link or phone the attendee supplied
            kind = next((k for k in options if k in ATTENDEE_LOCATIONS), None)
            return {"type": kind, ATTENDEE_LOCATIONS[kind]: detail} if kind else None
        value = str(response.get("value") or "")
        if value.startswith("integrations:"):  # e.g. integrations:daily is Cal Video
            name = {"daily": "cal-video", "google:meet": "google-meet"}.get(value.removeprefix("integrations:"), value.removeprefix("integrations:"))
            return {"type": "integration", "integration": name} if name in options else None
        return {"type": value} if value in options else None

    async def drop_named_type(booking: dict[str, Any]) -> None:
        """Delete a named meeting's one-off event type once no upcoming meeting uses it."""
        et = booking.get("eventType") or {}
        if not str(et.get("slug") or "").startswith(NAMED_SLUG_PREFIX):
            return
        with contextlib.suppress(CalComError):
            if not await cal.list_bookings(event_type_id=et["id"], status="upcoming"):
                await cal.delete_event_type(et["id"])
                event_types.pop(et["id"], None)

    async def slot_is_open(event_type_id: int, start: datetime, duration: int | None, reschedule_uid: str | None) -> bool:
        # cal.com only returns slots that fit entirely inside the window, so give it room for the meeting.
        slots = await cal.get_slots(
            event_type_id=event_type_id,
            start=tu.to_utc_z(start - timedelta(minutes=1)),
            end=tu.to_utc_z(start + timedelta(days=1)),
            time_zone="UTC",
            duration=duration,
            booking_uid_to_reschedule=reschedule_uid,
        )
        return start in _slot_starts(slots)

    async def book_named(base: dict[str, Any], title: str, length: int | None, begin: datetime, **booking: Any) -> Any:
        """Book on a hidden one-off copy of `base` named `title`, so the booking carries that name."""
        named = await call_cal(cal.create_event_type({
            "title": (title.strip() or base.get("title") or "Meeting")[:100],
            "slug": NAMED_SLUG_PREFIX + uuid.uuid4().hex[:10],
            "lengthInMinutes": length or base.get("lengthInMinutes") or base.get("length"),
            "hidden": True,
            "description": f"Booked by LiveX.AI Assistant from \"{base.get('title')}\".",
            "customName": "{Event type title}",  # the booking's title is exactly the name
            **{k: base[k] for k in COPIED_SETTINGS if base.get(k) is not None},
        }))
        try:
            return await call_cal(cal.create_booking(event_type_id=named["id"], start=tu.to_utc_z(begin), **booking))
        except ToolError:
            with contextlib.suppress(CalComError):
                await cal.delete_event_type(named["id"])  # don't leave an unused one-off type behind
            raise

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
                    "duration_options": et.get("lengthInMinutesOptions") or [],
                    "location_options": [
                        {"option": key, "value": loc.get("address") or loc.get("link") or loc.get("phone")}
                        for key, loc in location_options(et).items()
                    ],
                    "hidden": bool(et.get("hidden")),
                }
                for et in event_types
                if not str(et.get("slug") or "").startswith(NAMED_SLUG_PREFIX)
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
        duration_minutes: Annotated[int | None, Field(description="Only for event types with duration_options.")] = None,
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
        duration = await call_cal(duration_for(event_type_id, duration_minutes))

        slots = await call_cal(
            cal.get_slots(
                event_type_id=event_type_id,
                start=tu.to_utc_z(start),
                end=tu.to_utc_z(end),
                time_zone=timezone,
                duration=duration,
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
            Literal["upcoming", "recurring", "past", "cancelled", "unconfirmed"] | None,
            Field(description="Omit for all active (non-cancelled) bookings in the date range; defaults to upcoming when no dates are given."),
        ] = None,
        attendee_name: str | None = None,
        attendee_email: str | None = None,
        event_type_id: Annotated[int | None, Field(description="Only bookings of this event type, e.g. all intro calls.")] = None,
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
            cal.list_bookings(
                status=status, after_start=after, before_end=before, attendee_name=attendee_name,
                attendee_email=attendee_email, event_type_id=event_type_id,
            )
        )
        if status is None:
            bookings = [b for b in bookings if b.get("status") not in ("cancelled", "rejected")]
        items = sorted((compact_booking(b, tz) for b in bookings), key=lambda b: b["start"])
        return {"timezone": timezone, "count": len(items), "bookings": items}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def create_booking(
        event_type_id: int,
        start: Annotated[str, Field(description="Slot `start` from find_available_slots (UTC ISO 8601).")],
        title: Annotated[str, Field(description="The meeting's name: the user's, or a short one from the conversation, e.g. 'Intro call with Andy'.")],
        attendee_name: str,
        attendee_email: str,
        attendee_timezone: Annotated[str, Field(description="Attendee's IANA timezone; use the user's if unknown.")],
        duration_minutes: Annotated[int | None, Field(description="Only for event types with duration_options.")] = None,
        location: Annotated[str | None, Field(description="One of the event type's location_options. Omit for its default.")] = None,
        location_detail: Annotated[str | None, Field(description="The place, link or phone number, for attendeeAddress / attendeeDefined / attendeePhone.")] = None,
        guests: Annotated[list[str] | None, Field(description="Extra guest emails to invite.")] = None,
        notes: str | None = None,
        allow_conflicts: Annotated[bool, Field(description="Book even though the user is busy then. Only when the user explicitly asks to double-book.")] = False,
    ) -> dict[str, Any]:
        """Book a meeting with an attendee, under the given name. The slot is re-checked against live availability first."""
        if not attendee_name.strip():
            raise ToolError("attendee_name is required — ask the user who the meeting is with.")
        if not EMAIL_RE.match(attendee_email.strip()):
            raise ToolError(f"{attendee_email!r} doesn't look like an email — ask the user for it.")
        try:
            tz = tu.zone(attendee_timezone)
        except ValueError as e:
            raise ToolError(str(e))
        guest_list = [g.strip() for g in guests or [] if g.strip()]
        if bad := [g for g in guest_list if not EMAIL_RE.match(g)]:
            raise ToolError(f"These guest emails don't look valid: {', '.join(bad)}. Ask the user to check them.")
        where = await call_cal(booking_location(event_type_id, location, location_detail))
        begin = parse_start(start)
        duration = await call_cal(duration_for(event_type_id, duration_minutes))
        if not allow_conflicts and not await call_cal(slot_is_open(event_type_id, begin, duration, None)):
            raise ToolError("That slot is no longer available. Call find_available_slots again and offer new times.")
        booking = await book_named(
            await call_cal(event_type(event_type_id)),
            title,
            duration,
            begin,
            attendee={"name": attendee_name.strip(), "email": attendee_email.strip(), "timeZone": attendee_timezone},
            location=where,
            guests=guest_list or None,
            notes=notes,
            allow_conflicts=allow_conflicts,
        )
        # A recurring event type books a series and returns every occurrence.
        occurrences = booking if isinstance(booking, list) else [booking]
        result: dict[str, Any] = {"booked": compact_booking(occurrences[0], tz)}
        if len(occurrences) > 1:
            result["occurrences"] = [compact_booking(b, tz)["when"] for b in occurrences]
        return result

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
        duration = None
        if event_type_id:
            # Keep the booking's own length when its event type offers several.
            options = (await call_cal(event_type(event_type_id))).get("lengthInMinutesOptions") or []
            if booking.get("duration") in options:
                duration = booking["duration"]
        if event_type_id and not await call_cal(slot_is_open(event_type_id, begin, duration, booking_uid)):
            raise ToolError("That time isn't available. Call find_available_slots with reschedule_booking_uid and offer new times.")
        moved = await call_cal(
            cal.reschedule_booking(booking_uid, start=tu.to_utc_z(begin), rescheduled_by=await host_email(), reason=reason)
        )
        return {"previous_uid": booking_uid, "rescheduled": compact_booking(moved, tz)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    async def update_booking(
        booking_uid: str,
        timezone: TzArg,
        title: Annotated[str | None, Field(description="The meeting's new name. Omit to keep it.")] = None,
        attendee_name: Annotated[str | None, Field(description="The attendee's corrected name. Omit to keep it.")] = None,
        attendee_email: Annotated[str | None, Field(description="The attendee's corrected email. Omit to keep it.")] = None,
    ) -> dict[str, Any]:
        """Fix a meeting's name or its attendee's name/email. cal.com can't edit these, so this books an identical
        meeting (same time, length, location, guests, notes) with the corrections, then cancels the original.
        The attendee gets a cancellation and a new invite. Returns the NEW uid; use it for any follow-up."""
        try:
            tz = tu.zone(timezone)
        except ValueError as e:
            raise ToolError(str(e))
        old = await call_cal(active_booking(booking_uid))
        begin = tu.parse_iso(old["start"])
        if begin <= datetime.now(UTC):
            raise ToolError("That meeting has already started, so it can't be changed.")
        attendee = (old.get("attendees") or [{}])[0]
        name = (attendee_name or attendee.get("name") or "").strip()
        email = (attendee_email or attendee.get("email") or "").strip()
        if not name:
            raise ToolError("attendee_name is required — ask the user who the meeting is with.")
        if not EMAIL_RE.match(email):
            raise ToolError(f"{email!r} doesn't look like an email — ask the user for it.")
        if (title or old.get("title"), name, email) == (old.get("title"), attendee.get("name"), attendee.get("email")):
            raise ToolError("Nothing to change: the name, attendee and email are already as given.")
        event_type_id = (old.get("eventType") or {}).get("id") or old.get("eventTypeId")
        if not event_type_id:
            raise ToolError("This booking's event type no longer exists, so it can't be copied. Cancel it and book a new one.")
        base = await call_cal(event_type(event_type_id))
        responses = old.get("bookingFieldsResponses") or {}
        new = await book_named(
            base,
            title or old.get("title") or "",
            old.get("duration"),
            begin,
            attendee={"name": name, "email": email, "timeZone": attendee.get("timeZone") or timezone},
            location=same_location(base, responses.get("location")),
            guests=old.get("guests") or None,
            notes=responses.get("notes"),
            allow_conflicts=True,  # the original still holds this slot until it's cancelled below
        )
        new = new[0] if isinstance(new, list) else new
        result = {"previous_uid": booking_uid, "updated": compact_booking(new, tz)}
        try:
            await cal.cancel_booking(booking_uid, reason="Replaced by a corrected booking.")
        except CalComError as e:
            return {**result, "warning": f"The corrected meeting is booked, but the original couldn't be cancelled ({e.message}). Cancel {booking_uid!r} separately."}
        await drop_named_type(old)
        return result

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
        await drop_named_type(booking)
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
