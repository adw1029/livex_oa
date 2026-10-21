"""Timezone helpers. All date math lives here so the LLM never converts times itself."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Local-time windows for vague requests like "Thursday afternoon".
PARTS_OF_DAY: dict[str, tuple[int, int]] = {
    "morning": (8, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
}


def zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Unknown timezone {tz!r}; use an IANA name like 'America/Los_Angeles'.") from e


def parse_iso(value: str) -> datetime:
    """Parse cal.com timestamps ('2026-10-23T21:00:00.000Z', '...-07:00') into aware datetimes."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"Expected a date like 2026-10-23, got {value!r}.") from e


def local_day_bounds(day_from: date, day_to: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day_from, time.min, tzinfo=tz)
    end = datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def _clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {suffix}"


def fmt_day(dt: datetime | date) -> str:
    return f"{dt.strftime('%a %b')} {dt.day}"


def fmt_local(dt: datetime, tz: ZoneInfo) -> str:
    """'Thu Oct 23, 2:00 PM'"""
    local = dt.astimezone(tz)
    return f"{fmt_day(local)}, {_clock(local)}"


def fmt_range(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    """'Thu Oct 23, 2:00–2:30 PM PDT'"""
    s, e = start.astimezone(tz), end.astimezone(tz)
    first = _clock(s)
    if (s.hour < 12) == (e.hour < 12):
        first = first[:-3]  # "2:00–2:30 PM", not "2:00 PM–2:30 PM"
    return f"{fmt_day(s)}, {first}–{_clock(e)} {e.tzname()}"


def time_context(tz_name: str, now: datetime | None = None, days: int = 14) -> str:
    """A compact 'what day is it' note so the model resolves 'Thursday' or 'tomorrow' without guessing."""
    tz = zone(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    rows = []
    for i in range(days):
        d = (now + timedelta(days=i)).date()
        tag = " (today)" if i == 0 else " (tomorrow)" if i == 1 else ""
        rows.append(f"{d.isoformat()} {d.strftime('%a')}{tag}")
    return (
        f"User timezone: {tz_name} ({now.tzname()})\n"
        f"Now: {fmt_day(now)} {now.year}, {_clock(now)} (local time {now.strftime('%H:%M')})\n"
        f"Next {days} days: " + " | ".join(rows)
    )
