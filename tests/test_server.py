"""MCP server tools against a fake cal.com: time handling and write guards."""

from __future__ import annotations

from datetime import timedelta

import pytest
from mcp import Client

from calcom_mcp import timeutil as tu
from calcom_mcp.client import CalComError
from calcom_mcp.server import create_server
from tests.fakes import FakeCal, next_weekday

pytestmark = pytest.mark.anyio

TZ = "America/Los_Angeles"


@pytest.fixture
def cal() -> FakeCal:
    return FakeCal(next_weekday(3))  # a Thursday


@pytest.fixture
async def mcp(cal):
    async with Client(create_server(cal)) as client:
        yield client


async def test_tool_annotations_mark_writes(mcp):
    tools = {t.name: t.annotations for t in (await mcp.list_tools()).tools}
    assert tools["find_available_slots"].read_only_hint is True
    assert tools["list_bookings"].read_only_hint is True
    assert tools["create_booking"].read_only_hint is False
    assert tools["cancel_booking"].destructive_hint is True
    assert tools["reschedule_booking"].destructive_hint is True
    assert tools["update_booking"].destructive_hint is True


async def test_afternoon_slots_are_local_and_utc(mcp, cal):
    r = await mcp.call_tool(
        "find_available_slots",
        {"event_type_id": 1, "date_from": cal.day.isoformat(), "timezone": TZ, "part_of_day": "afternoon"},
    )
    assert not r.is_error
    (day,) = r.structured_content["days"]
    labels = [s["label"] for s in day["slots"]]
    assert labels[0].endswith("12:00 PM") and labels[-1].endswith("4:30 PM")
    assert len(labels) == 10
    assert all(s["start"].endswith("Z") for s in day["slots"])
    assert tu.parse_iso(day["slots"][0]["start"]) == cal.at(12)


async def test_after_time_filters_later_today(mcp, cal):
    r = await mcp.call_tool(
        "find_available_slots",
        {"event_type_id": 1, "date_from": cal.day.isoformat(), "timezone": TZ, "after_time": "15:00"},
    )
    starts = [tu.parse_iso(s["start"]) for s in r.structured_content["days"][0]["slots"]]
    assert min(starts) == cal.at(15)


async def test_create_rejects_taken_slot(mcp, cal):
    cal.seed("Board sync", cal.at(14))
    r = await mcp.call_tool(
        "create_booking",
        {
            "event_type_id": 1,
            "start": tu.to_utc_z(cal.at(14)),
            "title": "Intro call with Jordan",
            "attendee_name": "Jordan Lee",
            "attendee_email": "jordan@example.com",
            "attendee_timezone": TZ,
        },
    )
    assert r.is_error and "no longer available" in r.content[0].text
    assert not [c for c in cal.calls if c[0] == "create_booking"]


async def test_create_requires_real_email(mcp, cal):
    r = await mcp.call_tool(
        "create_booking",
        {
            "event_type_id": 1,
            "start": tu.to_utc_z(cal.at(14)),
            "title": "Intro call with Jordan",
            "attendee_name": "Jordan Lee",
            "attendee_email": "jordan",
            "attendee_timezone": TZ,
        },
    )
    assert r.is_error and "email" in r.content[0].text


async def test_create_books_open_slot(mcp, cal):
    r = await mcp.call_tool(
        "create_booking",
        {
            "event_type_id": 1,
            "start": tu.to_utc_z(cal.at(14)),
            "title": "Intro call with Jordan",
            "attendee_name": "Jordan Lee",
            "attendee_email": "jordan@example.com",
            "attendee_timezone": TZ,
        },
    )
    assert not r.is_error
    booked = r.structured_content["booked"]
    assert booked["when"].endswith("2:00–2:30 PM " + cal.at(14).tzname())
    assert booked["attendees"] == [{"name": "Jordan Lee", "email": "jordan@example.com"}]


async def test_reschedule_returns_new_uid_and_auto_confirms(mcp, cal):
    uid = cal.seed("Intro with Sam", cal.at(15))
    r = await mcp.call_tool(
        "reschedule_booking", {"booking_uid": uid, "new_start": tu.to_utc_z(cal.at(16)), "timezone": TZ}
    )
    assert not r.is_error
    assert r.structured_content["previous_uid"] == uid
    assert r.structured_content["rescheduled"]["uid"] != uid
    ((_, args),) = [c for c in cal.calls if c[0] == "reschedule_booking"]
    assert args["rescheduled_by"] == "founder@example.com"


async def test_cancel_guards_unknown_and_already_cancelled(mcp, cal):
    r = await mcp.call_tool("cancel_booking", {"booking_uid": "nope", "timezone": TZ})
    assert r.is_error and "No booking" in r.content[0].text

    uid = cal.seed("Intro with Sam", cal.at(10))
    assert not (await mcp.call_tool("cancel_booking", {"booking_uid": uid, "timezone": TZ})).is_error
    assert cal.calls[-1] == ("cancel_booking", {"uid": uid, "reason": "Cancelled by the host."})  # cal.com requires one
    again = await mcp.call_tool("cancel_booking", {"booking_uid": uid, "timezone": TZ})
    assert again.is_error and "already cancelled" in again.content[0].text


async def test_list_bookings_for_a_day_hides_cancelled(mcp, cal):
    keep = cal.seed("Keep", cal.at(10))
    gone = cal.seed("Gone", cal.at(11))
    cal.bookings[gone]["status"] = "cancelled"
    r = await mcp.call_tool("list_bookings", {"timezone": TZ, "date_from": cal.day.isoformat()})
    assert [b["uid"] for b in r.structured_content["bookings"]] == [keep]


async def test_bad_timezone_is_a_clear_error(mcp, cal):
    r = await mcp.call_tool("list_bookings", {"timezone": "Mars/Olympus"})
    assert r.is_error and "IANA" in r.content[0].text


async def test_duration_only_sent_for_multi_length_event_types(mcp, cal):
    book = {
        "start": tu.to_utc_z(cal.at(14)),
        "title": "Intro call with Jordan",
        "attendee_name": "Jordan Lee",
        "attendee_email": "jordan@example.com",
        "attendee_timezone": TZ,
    }
    # Fixed 30-min type: a matching duration is dropped (cal.com rejects it), a different one is refused.
    assert not (await mcp.call_tool("create_booking", {**book, "event_type_id": 1, "duration_minutes": 30})).is_error
    assert cal.calls[-1][1]["length_in_minutes"] is None
    r = await mcp.call_tool(
        "create_booking", {**book, "start": tu.to_utc_z(cal.at(15)), "event_type_id": 1, "duration_minutes": 60}
    )
    assert r.is_error and "always 30 minutes" in r.content[0].text

    # Multi-length type: an offered duration goes to both the slot search and the booking.
    r = await mcp.call_tool(
        "find_available_slots",
        {"event_type_id": 2, "date_from": cal.day.isoformat(), "timezone": TZ, "duration_minutes": 60},
    )
    assert not r.is_error and [c for c in cal.calls if c[0] == "get_slots"][-1][1]["duration"] == 60
    assert not (
        await mcp.call_tool(
            "create_booking", {**book, "start": tu.to_utc_z(cal.at(16)), "event_type_id": 2, "duration_minutes": 60}
        )
    ).is_error
    named = [c for c in cal.calls if c[0] == "create_event_type"][-1][1]
    assert named["lengthInMinutes"] == 60  # the named meeting's own event type carries the length
    r = await mcp.call_tool(
        "create_booking", {**book, "start": tu.to_utc_z(cal.at(10)), "event_type_id": 2, "duration_minutes": 45}
    )
    assert r.is_error and "15, 30, 60" in r.content[0].text


async def test_stale_uid_after_reschedule_points_to_new_booking(mcp, cal):
    uid = cal.seed("Intro with Sam", cal.at(15))
    moved = await mcp.call_tool(
        "reschedule_booking", {"booking_uid": uid, "new_start": tu.to_utc_z(cal.at(16)), "timezone": TZ}
    )
    new_uid = moved.structured_content["rescheduled"]["uid"]
    r = await mcp.call_tool("cancel_booking", {"booking_uid": uid, "timezone": TZ})
    assert r.is_error and new_uid in r.content[0].text


async def test_list_bookings_filters_by_attendee_email_and_event_type(mcp, cal):
    cal.seed("Intro with Sam", cal.at(10))
    jordan = cal.seed("Intro with Jordan", cal.at(11), attendee="Jordan Lee")
    r = await mcp.call_tool(
        "list_bookings", {"timezone": TZ, "attendee_email": "jordan@example.com", "event_type_id": 1}
    )
    assert [b["uid"] for b in r.structured_content["bookings"]] == [jordan]
    assert cal.calls[-1] == (
        "list_bookings",
        {"status": "upcoming", "attendee_email": "jordan@example.com", "event_type_id": 1},
    )


BOOK = {
    "title": "Intro call with Jordan",
    "attendee_name": "Jordan Lee",
    "attendee_email": "jordan@example.com",
    "attendee_timezone": TZ,
}


async def test_location_must_be_one_the_event_type_allows(mcp, cal):
    r = await mcp.call_tool("list_event_types", {})
    office = r.structured_content["event_types"][1]
    assert office["location_options"] == [
        {"option": "zoom", "value": None},
        {"option": "address", "value": "1 Market St"},
        {"option": "attendeeDefined", "value": None},
    ]
    # The 30 Min Intro only offers Cal Video, so a custom place is refused with the allowed options.
    r = await mcp.call_tool(
        "create_booking",
        {
            **BOOK,
            "event_type_id": 1,
            "start": tu.to_utc_z(cal.at(14)),
            "location": "attendeeDefined",
            "location_detail": "Blue Bottle",
        },
    )
    assert r.is_error and "cal-video" in r.content[0].text
    # Attendee-defined places need the place itself.
    r = await mcp.call_tool(
        "create_booking", {**BOOK, "event_type_id": 2, "start": tu.to_utc_z(cal.at(14)), "location": "attendeeDefined"}
    )
    assert r.is_error and "Ask the user for the location" in r.content[0].text

    await mcp.call_tool(
        "create_booking",
        {
            **BOOK,
            "event_type_id": 2,
            "start": tu.to_utc_z(cal.at(14)),
            "location": "attendeeDefined",
            "location_detail": "Blue Bottle, Market St",
        },
    )
    assert cal.calls[-1][1]["location"] == {"type": "attendeeDefined", "location": "Blue Bottle, Market St"}
    await mcp.call_tool(
        "create_booking", {**BOOK, "event_type_id": 2, "start": tu.to_utc_z(cal.at(15)), "location": "zoom"}
    )
    assert cal.calls[-1][1]["location"] == {"type": "integration", "integration": "zoom"}


async def test_guests_are_validated_and_passed_through(mcp, cal):
    r = await mcp.call_tool(
        "create_booking",
        {**BOOK, "event_type_id": 1, "start": tu.to_utc_z(cal.at(14)), "guests": ["sam@", "ok@example.com"]},
    )
    assert r.is_error and "sam@" in r.content[0].text
    r = await mcp.call_tool(
        "create_booking", {**BOOK, "event_type_id": 1, "start": tu.to_utc_z(cal.at(14)), "guests": ["sam@example.com"]}
    )
    assert not r.is_error and cal.calls[-1][1]["guests"] == ["sam@example.com"]


async def test_allow_conflicts_books_over_an_existing_meeting(mcp, cal):
    cal.seed("Board sync", cal.at(14))
    args = {**BOOK, "event_type_id": 1, "start": tu.to_utc_z(cal.at(14))}
    assert (await mcp.call_tool("create_booking", args)).is_error
    r = await mcp.call_tool("create_booking", {**args, "allow_conflicts": True})
    assert not r.is_error and cal.calls[-1][1]["allow_conflicts"] is True


async def test_recurring_event_type_returns_every_occurrence(mcp, cal):
    async def create_series(**kwargs):
        first = tu.parse_iso(kwargs["start"])
        return [cal._booking(f"r{i}", "Weekly 1:1", first + timedelta(weeks=i), "Jordan Lee") for i in range(3)]

    cal.create_booking = create_series
    r = await mcp.call_tool("create_booking", {**BOOK, "event_type_id": 1, "start": tu.to_utc_z(cal.at(14))})
    assert not r.is_error
    assert r.structured_content["booked"]["uid"] == "r0" and len(r.structured_content["occurrences"]) == 3


async def test_meeting_is_booked_under_its_name(mcp, cal):
    r = await mcp.call_tool(
        "create_booking",
        {**BOOK, "title": "Investor sync", "event_type_id": 2, "start": tu.to_utc_z(cal.at(14)), "location": "zoom"},
    )
    assert not r.is_error and r.structured_content["booked"]["title"] == "Investor sync"
    named = next(c[1] for c in cal.calls if c[0] == "create_event_type")
    assert named["title"] == "Investor sync" and named["hidden"] and named["customName"] == "{Event type title}"
    assert named["locations"] == cal.event_types[1]["locations"]  # same options as the type the user picked
    # One-off types never show up as bookable event types.
    listed = (await mcp.call_tool("list_event_types", {})).structured_content["event_types"]
    assert [e["title"] for e in listed] == ["30 Min Intro", "Office Hours"]


async def test_failed_booking_removes_its_one_off_event_type(mcp, cal):
    async def reject(**kwargs):
        raise CalComError(400, "attendee email is not allowed")

    cal.create_booking = reject
    r = await mcp.call_tool("create_booking", {**BOOK, "event_type_id": 1, "start": tu.to_utc_z(cal.at(14))})
    assert r.is_error and "not allowed" in r.content[0].text
    assert [c[0] for c in cal.calls[-2:]] == ["create_event_type", "delete_event_type"]
    assert len(cal.event_types) == 2


async def test_cancelling_a_named_meeting_removes_its_one_off_type(mcp, cal):
    r = await mcp.call_tool(
        "create_booking", {**BOOK, "title": "Investor sync", "event_type_id": 1, "start": tu.to_utc_z(cal.at(14))}
    )
    uid = r.structured_content["booked"]["uid"]
    moved = await mcp.call_tool(
        "reschedule_booking", {"booking_uid": uid, "new_start": tu.to_utc_z(cal.at(15)), "timezone": TZ}
    )
    assert not [c for c in cal.calls if c[0] == "delete_event_type"]  # still in use after the move
    await mcp.call_tool(
        "cancel_booking", {"booking_uid": moved.structured_content["rescheduled"]["uid"], "timezone": TZ}
    )
    assert [c[0] for c in cal.calls].count("delete_event_type") == 1
    assert [e["title"] for e in cal.event_types] == ["30 Min Intro", "Office Hours"]


async def test_update_rebooks_the_same_meeting_with_corrections(mcp, cal):
    r = await mcp.call_tool(
        "create_booking",
        {
            **BOOK,
            "event_type_id": 2,
            "start": tu.to_utc_z(cal.at(14)),
            "duration_minutes": 60,
            "location": "attendeeDefined",
            "location_detail": "Blue Bottle",
            "guests": ["ops@example.com"],
            "notes": "bring deck",
        },
    )
    old = r.structured_content["booked"]
    r = await mcp.call_tool(
        "update_booking", {"booking_uid": old["uid"], "timezone": TZ, "attendee_email": "jordan@lee.dev"}
    )
    assert not r.is_error, r.content[0].text
    new = r.structured_content["updated"]
    assert r.structured_content["previous_uid"] == old["uid"] and new["uid"] != old["uid"]
    assert (new["start"], new["duration_minutes"], new["title"]) == (old["start"], 60, "Intro call with Jordan")
    assert new["attendees"] == [{"name": "Jordan Lee", "email": "jordan@lee.dev"}]
    rebook = [c[1] for c in cal.calls if c[0] == "create_booking"][-1]
    assert rebook["location"] == {"type": "attendeeDefined", "location": "Blue Bottle"}
    assert (
        rebook["guests"] == ["ops@example.com"] and rebook["allow_conflicts"] is True
    )  # the original holds the slot until cancelled
    assert cal.bookings[old["uid"]]["status"] == "cancelled"
    assert [e["title"] for e in cal.event_types] == [
        "30 Min Intro",
        "Office Hours",
        "Intro call with Jordan",
    ]  # old one-off removed


async def test_update_renames_and_guards(mcp, cal):
    uid = cal.seed("30 Min Intro between Founder and Sam", cal.at(10))
    same = await mcp.call_tool("update_booking", {"booking_uid": uid, "timezone": TZ, "attendee_name": "Sam Rivera"})
    assert same.is_error and "Nothing to change" in same.content[0].text
    bad = await mcp.call_tool("update_booking", {"booking_uid": uid, "timezone": TZ, "attendee_email": "sam@"})
    assert bad.is_error and "doesn't look like an email" in bad.content[0].text
    r = await mcp.call_tool("update_booking", {"booking_uid": uid, "timezone": TZ, "title": "Design review"})
    assert r.structured_content["updated"]["title"] == "Design review"
    assert cal.bookings[uid]["status"] == "cancelled"
    again = await mcp.call_tool("update_booking", {"booking_uid": uid, "timezone": TZ, "title": "x"})
    assert again.is_error and "already cancelled" in again.content[0].text
