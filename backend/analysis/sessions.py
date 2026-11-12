"""Asian / London / New York market sessions, measured from real 5-minute candles.

Session hours follow each centre's local clock, so daylight-saving shifts are
handled automatically (London and New York move by an hour in Mar/Oct–Nov;
Tokyo never does). All times are reported in UTC and IST.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from backend.timeutils import iso, ist_label

SESSIONS = (
    # key, display name, time zone, local open, local close
    ("ASIAN", "Asian (Tokyo)", "Asia/Tokyo", time(9, 0), time(18, 0)),
    ("LONDON", "London", "Europe/London", time(8, 0), time(17, 0)),
    ("NEW_YORK", "New York", "America/New_York", time(8, 0), time(17, 0)),
)
FLAT_PCT = 0.05   # a session move smaller than this % of price is called FLAT


def _window(tz_name: str, local_day, open_t: time, close_t: time) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(local_day, open_t, tzinfo=tz)
    end = datetime.combine(local_day, close_t, tzinfo=tz)
    return start.astimezone(ZoneInfo("UTC")), end.astimezone(ZoneInfo("UTC"))


def latest_window(tz_name: str, open_t: time, close_t: time, now: datetime) -> tuple[datetime, datetime]:
    """Most recent weekday session that has already started."""
    local_today = now.astimezone(ZoneInfo(tz_name)).date()
    for back in range(0, 8):
        day = local_today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        start, end = _window(tz_name, day, open_t, close_t)
        if start <= now:
            return start, end
    raise RuntimeError("no session found in the last week")


def next_window_start(tz_name: str, open_t: time, close_t: time, now: datetime) -> datetime:
    local_today = now.astimezone(ZoneInfo(tz_name)).date()
    for ahead in range(0, 8):
        day = local_today + timedelta(days=ahead)
        if day.weekday() >= 5:
            continue
        start, _ = _window(tz_name, day, open_t, close_t)
        if start > now:
            return start
    raise RuntimeError("no upcoming session found")


def session_stats(m5: pd.DataFrame, start: datetime, end: datetime, data_until: datetime | None,
                  digits: int = 2) -> dict:
    s = m5[(m5.index >= pd.Timestamp(start)) & (m5.index < pd.Timestamp(end))]
    if s.empty:
        return {"has_data": False}
    first_open = s["open"].dropna()
    open_px = float(first_open.iloc[0]) if len(first_open) else float(s["close"].iloc[0])
    high, low, last = float(s["high"].max()), float(s["low"].min()), float(s["close"].iloc[-1])
    change = last - open_px
    pct = change / open_px * 100
    direction = "FLAT" if abs(pct) < FLAT_PCT else ("UP" if change > 0 else "DOWN")
    hi_t = s["high"].idxmax().to_pydatetime()
    lo_t = s["low"].idxmin().to_pydatetime()
    covered_to = min(end, data_until) if data_until else end
    return {
        "has_data": True,
        "open": round(open_px, digits),
        "high": round(high, digits),
        "low": round(low, digits),
        "last": round(last, digits),
        "range": round(high - low, digits),
        "change": round(change, digits),
        "change_pct": round(pct, 3),
        "direction": direction,
        "high_time_ist": ist_label(hi_t),
        "low_time_ist": ist_label(lo_t),
        "data_until_utc": iso(covered_to),
        "data_until_ist": ist_label(covered_to),
        "candles": int(len(s)),
    }


def build_sessions(m5: pd.DataFrame, now: datetime, data_until: datetime | None) -> list[dict]:
    out = []
    for key, name, tz_name, o, c in SESSIONS:
        start, end = latest_window(tz_name, o, c, now)
        status = "OPEN" if now < end else "CLOSED"
        nxt = next_window_start(tz_name, o, c, now)
        out.append(
            {
                "key": key,
                "name": name,
                "local_hours": f"{o:%H:%M}–{c:%H:%M} {tz_name.split('/')[-1].replace('_', ' ')} time",
                "status": status,
                "start_utc": iso(start),
                "end_utc": iso(end),
                "start_ist": ist_label(start),
                "end_ist": ist_label(end),
                "next_start_ist": ist_label(nxt),
                **session_stats(m5, start, end, data_until),
            }
        )
    return out
