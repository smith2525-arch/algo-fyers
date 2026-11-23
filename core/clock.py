"""Strict Asia/Kolkata (IST, +05:30) clock utilities and protocols.

Every timestamp across core domain models, database sessions, structured logs,
API payloads, and the Streamlit UI operates exclusively in IST (+05:30).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    IST: timezone | ZoneInfo = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

IST_TIMEZONE_NAME = "Asia/Kolkata"


def now_ist() -> datetime:
    """Return the current timezone-aware timestamp in Asia/Kolkata (IST, +05:30)."""
    return datetime.now(IST)


def ensure_ist(value: datetime) -> datetime:
    """Normalize any datetime into timezone-aware Asia/Kolkata (IST, +05:30).

    Naive datetimes are interpreted as already representing wall-clock IST and
    tagged with IST tzinfo; aware datetimes from other zones are converted to IST.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def format_ist(value: datetime | None, *, include_ms: bool = False) -> str:
    """Format a datetime consistently in IST for operational display."""
    if value is None:
        return "—"
    dt = ensure_ist(value)
    if include_ms:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " IST"
    return dt.strftime("%Y-%m-%d %H:%M:%S IST")


def format_ist_iso(value: datetime) -> str:
    """Format a datetime as ISO-8601 with explicit +05:30 offset."""
    return ensure_ist(value).isoformat(timespec="milliseconds")


def parse_hms_time(hms: str) -> time:
    """Parse HH:MM:SS session boundary string."""
    parts = [int(part) for part in hms.strip().split(":")]
    if len(parts) == 2:
        return time(hour=parts[0], minute=parts[1], second=0)
    if len(parts) == 3:
        return time(hour=parts[0], minute=parts[1], second=parts[2])
    raise ValueError(f"Invalid HH:MM[:SS] time string: {hms!r}")


def is_within_ist_session(
    moment: datetime,
    session_open_hms: str = "09:15:00",
    session_close_hms: str = "15:30:00",
) -> bool:
    """Check whether an IST timestamp falls within regular weekday market hours."""
    ist_dt = ensure_ist(moment)
    if ist_dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_t = parse_hms_time(session_open_hms)
    close_t = parse_hms_time(session_close_hms)
    current_t = ist_dt.timetz().replace(tzinfo=None)
    return open_t <= current_t <= close_t


class TradingClock(Protocol):
    """Clock abstraction allowing deterministic testing and market replay."""

    def now(self) -> datetime:
        """Return current IST timestamp."""


class SystemISTClock:
    """Production wall-clock returning Asia/Kolkata (IST) timestamps."""

    def now(self) -> datetime:
        return now_ist()


class FixedISTClock:
    """Deterministic controllable IST clock for unit tests and replay engines."""

    def __init__(self, initial: datetime) -> None:
        self._current = ensure_ist(initial)

    def now(self) -> datetime:
        return self._current

    def set(self, moment: datetime) -> None:
        self._current = ensure_ist(moment)

    def advance(self, delta: timedelta) -> datetime:
        self._current = ensure_ist(self._current + delta)
        return self._current
