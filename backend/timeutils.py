"""Time helpers. All internal times are timezone-aware UTC.

IST is a fixed UTC+05:30 offset (India has no daylight saving), so we do not
need the tz database for it – this avoids the missing-tzdata problem on Windows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def from_epoch_utc(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def to_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime passed to to_ist(); expected UTC-aware")
    return dt.astimezone(IST)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def ist_label(dt: datetime | None) -> str | None:
    """Human-friendly IST string, e.g. '11 Nov 2026, 19:20:05 IST'."""
    if dt is None:
        return None
    return to_ist(dt).strftime("%d %b %Y, %H:%M:%S IST")


def weekend_closure_reason(now: datetime) -> str | None:
    """Rough check for the weekly gold market closure (Fri ~21:00 UTC to Sun ~22:00 UTC).

    Used only to explain *why* data is stale; it never hides a price.
    """
    now = now.astimezone(timezone.utc)
    wd, hour = now.weekday(), now.hour  # Monday=0 … Sunday=6
    if wd == 5 or (wd == 4 and hour >= 21) or (wd == 6 and hour < 22):
        return "Weekend – the gold market is closed (reopens Sunday ~22:00–23:00 UTC / Monday ~03:30–04:30 IST)."
    if 21 <= hour < 23 and wd in (0, 1, 2, 3, 6):
        return "Possibly the daily gold trading break (around 21:00–23:00 UTC / 02:30–04:30 IST)."
    return None
