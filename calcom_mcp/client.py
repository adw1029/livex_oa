"""Thin async client for the cal.com v2 REST API."""

from __future__ import annotations

import ssl
from typing import Any

import httpx
import truststore

BASE_URL = "https://api.cal.com/v2"

# cal.com versions each endpoint independently, and a missing or wrong
# `cal-api-version` silently falls back to an older response shape.
V_EVENT_TYPES = "2024-06-14"
V_SLOTS = "2024-09-04"
V_BOOKINGS_LIST = "2026-05-01"
V_BOOKINGS_WRITE = "2026-02-25"


class CalComError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"cal.com {status}: {message}")
        self.status = status
        self.message = message


def _error_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("details")
            if msg:
                return msg if isinstance(msg, str) else str(msg)
        if isinstance(body.get("message"), (str, list)):
            return str(body["message"])
    return fallback[:300] or "unknown error"


class CalClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
            # Verify TLS against the OS trust store, so corporate proxies (e.g. Zscaler) with a
            # system-installed root CA work the same as in the browser.
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        version: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        full: bool = False,
    ) -> Any:
        headers = {"cal-api-version": version} if version else {}
        params = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._http.request(method, path, params=params, json=json, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.is_error or (isinstance(body, dict) and body.get("status") == "error"):
            raise CalComError(resp.status_code, _error_message(body, resp.text))
        if full or not isinstance(body, dict):
            return body
        return body.get("data")

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def list_event_types(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/event-types", version=V_EVENT_TYPES) or []

    async def create_event_type(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/event-types", version=V_EVENT_TYPES, json=body)

    async def delete_event_type(self, event_type_id: int) -> None:
        await self._request("DELETE", f"/event-types/{event_type_id}", version=V_EVENT_TYPES)

    async def get_slots(
        self,
        *,
        event_type_id: int,
        start: str,
        end: str,
        time_zone: str,
        duration: int | None = None,
        booking_uid_to_reschedule: str | None = None,
    ) -> dict[str, list[Any]]:
        """Returns {"YYYY-MM-DD": [{"start": ..., "end": ...}, ...]} for the range (UTC bounds)."""
        return (
            await self._request(
                "GET",
                "/slots",
                version=V_SLOTS,
                params={
                    "eventTypeId": event_type_id,
                    "start": start,
                    "end": end,
                    "timeZone": time_zone,
                    "duration": duration,
                    "bookingUidToReschedule": booking_uid_to_reschedule,
                    "format": "range",
                },
            )
            or {}
        )

    async def list_bookings(
        self,
        *,
        status: str | None = None,
        after_start: str | None = None,
        before_end: str | None = None,
        attendee_name: str | None = None,
        attendee_email: str | None = None,
        event_type_id: int | None = None,
        booking_uid: str | None = None,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        """All matching bookings, following cal.com's cursor pagination (100 per page, up to `max_pages`)."""
        params: dict[str, Any] = {
            "status": status,
            "afterStart": after_start,
            "beforeEnd": before_end,
            "attendeeName": attendee_name,
            "attendeeEmail": attendee_email,
            "eventTypeId": event_type_id,
            "bookingUid": booking_uid,
            "sortStart": "asc",
            "limit": 100,
        }
        bookings: list[dict[str, Any]] = []
        for _ in range(max_pages):
            body = await self._request("GET", "/bookings", version=V_BOOKINGS_LIST, params=params, full=True)
            bookings += body.get("data") or []
            page = body.get("pagination") or {}
            if not (page.get("hasMore") and page.get("nextCursor")):
                break
            params["cursor"] = page["nextCursor"]
        return bookings

    async def create_booking(
        self,
        *,
        event_type_id: int,
        start: str,
        attendee: dict[str, Any],
        length_in_minutes: int | None = None,
        location: dict[str, Any] | None = None,
        guests: list[str] | None = None,
        notes: str | None = None,
        allow_conflicts: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Returns the booking, or a list of occurrences for a recurring event type."""
        body: dict[str, Any] = {"eventTypeId": event_type_id, "start": start, "attendee": attendee}
        if length_in_minutes:
            # Only valid for event types with several lengths; the server checks this before calling.
            body["lengthInMinutes"] = length_in_minutes
        if location:
            body["location"] = location  # must be one of the event type's locations
        if guests:
            body["guests"] = guests
        if notes:
            body["bookingFieldsResponses"] = {"notes": notes}
        if allow_conflicts:
            body["allowConflicts"] = True  # host-only override: book even if the host is busy
        return await self._request("POST", "/bookings", version=V_BOOKINGS_WRITE, json=body)

    async def reschedule_booking(
        self,
        uid: str,
        *,
        start: str,
        rescheduled_by: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"start": start}
        if rescheduled_by:
            # Passing the host's email auto-confirms the new booking.
            body["rescheduledBy"] = rescheduled_by
        if reason:
            body["reschedulingReason"] = reason
        return await self._request("POST", f"/bookings/{uid}/reschedule", version=V_BOOKINGS_WRITE, json=body)

    async def cancel_booking(self, uid: str, *, reason: str | None = None) -> dict[str, Any]:
        body = {"cancellationReason": reason} if reason else {}
        return await self._request("POST", f"/bookings/{uid}/cancel", version=V_BOOKINGS_WRITE, json=body)
