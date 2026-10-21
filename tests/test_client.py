"""cal.com HTTP client against a mocked transport: pagination and error handling."""

from __future__ import annotations

import httpx
import pytest

from calcom_mcp.client import BASE_URL, CalClient, CalComError

pytestmark = pytest.mark.anyio


def client(handler) -> CalClient:
    return CalClient("cal_test", http=httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)))


async def test_list_bookings_follows_cursor_pagination():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        assert request.headers["cal-api-version"] == "2026-05-01"
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"status": "success", "data": [{"uid": "a"}], "pagination": {"hasMore": True, "nextCursor": "c2"}})
        return httpx.Response(200, json={"status": "success", "data": [{"uid": "b"}], "pagination": {"hasMore": False, "nextCursor": None}})

    bookings = await client(handler).list_bookings(attendee_email="jordan@example.com", event_type_id=7)
    assert [b["uid"] for b in bookings] == ["a", "b"]
    assert seen[0]["attendeeEmail"] == "jordan@example.com" and seen[0]["eventTypeId"] == "7" and seen[0]["limit"] == "100"
    assert seen[1]["cursor"] == "c2"


async def test_errors_carry_cal_com_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"status": "error", "error": {"message": "Cancellation reason is required"}})

    with pytest.raises(CalComError, match="Cancellation reason is required"):
        await client(handler).cancel_booking("uid1")
