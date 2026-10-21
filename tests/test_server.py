"""MCP server tools against a fake cal.com: time handling and write guards."""

from __future__ import annotations

import pytest
from mcp import Client

from calcom_mcp import timeutil as tu
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
        {"event_type_id": 1, "start": tu.to_utc_z(cal.at(14)), "attendee_name": "Jordan Lee",
         "attendee_email": "jordan@example.com", "attendee_timezone": TZ},
    )
    assert r.is_error and "no longer available" in r.content[0].text
    assert not [c for c in cal.calls if c[0] == "create_booking"]


async def test_create_requires_real_email(mcp, cal):
    r = await mcp.call_tool(
        "create_booking",
        {"event_type_id": 1, "start": tu.to_utc_z(cal.at(14)), "attendee_name": "Jordan Lee",
         "attendee_email": "jordan", "attendee_timezone": TZ},
    )
    assert r.is_error and "email" in r.content[0].text


async def test_create_books_open_slot(mcp, cal):
    r = await mcp.call_tool(
        "create_booking",
        {"event_type_id": 1, "start": tu.to_utc_z(cal.at(14)), "attendee_name": "Jordan Lee",
         "attendee_email": "jordan@example.com", "attendee_timezone": TZ},
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
    (_, args), = [c for c in cal.calls if c[0] == "reschedule_booking"]
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
