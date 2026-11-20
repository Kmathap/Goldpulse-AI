"""Economic calendar (free, no API key): the ForexFactory weekly feed.

Gold reacts most to US data (CPI, NFP, FOMC, PCE, GDP), so the dashboard shows USD
events and turns them into a simple trade warning:

* NO_TRADE – a HIGH-impact USD event is within 30 minutes before, or 15 minutes after, now
* CAUTION  – a HIGH-impact USD event is within the next 2 hours
* CLEAR    – nothing high-impact for USD in the next 2 hours

The feed only covers the current week (Sunday–Saturday, New York time) and asks
users not to download it more than a few times per hour, so it is cached for
1 hour and a failed download is not retried for 10 minutes. The last good copy
is kept and shown if the feed is temporarily unavailable. If the feed has never
loaded (it often refuses shared cloud servers), the built-in official schedule in
backend/data/us_schedule.py is used instead, so the warning still works.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from backend.data.base import DataUnavailableError
from backend.data import us_schedule
from backend.data.http import HttpGet, make_http_get
from backend.timeutils import ist_label, iso

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE = "ForexFactory weekly calendar (free)"
CACHE_SECONDS = 3600
RETRY_AFTER_FAIL = 600
BLOCK_BEFORE_MIN = 30
BLOCK_AFTER_MIN = 15
CAUTION_BEFORE_MIN = 120
KEEP_PAST_MIN = 180          # show events that happened in the last 3 hours (gold may still be moving)
GOLD_CURRENCIES = ("USD",)   # gold is priced in US dollars
TOP_EVENTS = ("CPI", "Non-Farm", "FOMC", "Federal Funds Rate", "PCE", "GDP", "Fed Chair", "Unemployment Rate",
              "Retail Sales", "PPI", "ISM", "JOLTS", "Unemployment Claims")


def _parse(e: dict) -> dict | None:
    try:
        when = datetime.fromisoformat(str(e["date"])).astimezone(timezone.utc)
    except (KeyError, ValueError):
        return None
    title = str(e.get("title") or "").strip()
    return {
        "title": title,
        "currency": str(e.get("country") or "").upper(),
        "impact": str(e.get("impact") or "Low"),
        "time": when,
        "forecast": str(e.get("forecast") or "") or None,
        "previous": str(e.get("previous") or "") or None,
        "key_event": any(k.lower() in title.lower() for k in TOP_EVENTS),
    }


class CalendarProvider:
    def __init__(self, http_get: HttpGet | None = None, clock: Callable[[], float] = time.time):
        self._get = http_get or make_http_get(15.0)
        self._clock = clock
        self._lock = threading.Lock()
        self._events: list[dict] | None = None
        self._fetched_at: float | None = None
        self._next_try: float = 0.0
        self._last_error: str | None = None

    # ------------------------------------------------------------ download
    def _refresh(self) -> None:
        now = self._clock()
        fresh = self._fetched_at is not None and now - self._fetched_at < CACHE_SECONDS
        if fresh or now < self._next_try:
            return
        try:
            body: Any = self._get(FEED_URL, None)
            if not isinstance(body, list):
                raise DataUnavailableError("Calendar feed returned an unexpected format.")
            events = [p for p in (_parse(e) for e in body if isinstance(e, dict)) if p]
            events.sort(key=lambda x: x["time"])
            self._events, self._fetched_at, self._last_error = events, now, None
        except DataUnavailableError as exc:
            self._last_error = str(exc)
            self._next_try = now + RETRY_AFTER_FAIL

    # ------------------------------------------------------------ payload
    def payload(self) -> dict:
        with self._lock:
            self._refresh()
            events, fetched_at, error = self._events, self._fetched_at, self._last_error
        source, fallback = SOURCE, False
        if events is None:
            events = [p for p in (_parse(e) for e in us_schedule.as_feed()) if p]
            source, fallback = us_schedule.SOURCE, True

        now_ts = self._clock()
        now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        rows = []
        for e in events:
            if e["currency"] not in GOLD_CURRENCIES or e["impact"] not in ("High", "Medium"):
                continue
            minutes = round((e["time"] - now).total_seconds() / 60)
            if minutes < -KEEP_PAST_MIN:
                continue
            rows.append({
                "title": e["title"], "currency": e["currency"], "impact": e["impact"], "key_event": e["key_event"],
                "time_utc": iso(e["time"]), "time_ist": ist_label(e["time"]),
                "minutes_until": minutes, "forecast": e["forecast"], "previous": e["previous"],
            })

        high = [r for r in rows if r["impact"] == "High"]
        block = [r for r in high if -BLOCK_AFTER_MIN <= r["minutes_until"] <= BLOCK_BEFORE_MIN]
        caution = [r for r in high if BLOCK_BEFORE_MIN < r["minutes_until"] <= CAUTION_BEFORE_MIN]
        upcoming_high = [r for r in high if r["minutes_until"] > BLOCK_BEFORE_MIN]
        if block:
            e = block[0]
            when = f"in {e['minutes_until']} min" if e["minutes_until"] >= 0 else f"{-e['minutes_until']} min ago"
            warning = {"state": "NO_TRADE", "text": f"High-impact USD news {when}: {e['title']}. Do not open new gold trades "
                       f"from {BLOCK_BEFORE_MIN} min before until {BLOCK_AFTER_MIN} min after – price can jump and spreads widen."}
        elif caution:
            e = caution[0]
            warning = {"state": "CAUTION", "text": f"{e['title']} (high impact) in {e['minutes_until']} min. "
                       "Only enter if your stop and target can be reached before it, or wait until after."}
        else:
            nxt = upcoming_high[0] if upcoming_high else None
            warning = {"state": "CLEAR", "text": "No high-impact USD news in the next 2 hours."
                       + (f" Next: {nxt['title']} at {nxt['time_ist']}." if nxt else
                          " No more high-impact USD events this week (next week's calendar appears on Sunday).")}

        if fallback:
            stale = ("Live calendar feed unavailable, so the built-in official dates are shown (high-impact USD events only, "
                     "no forecasts)." + (f" Feed error: {error}" if error else ""))
            if now.date().isoformat() > us_schedule.LAST_DATE:
                stale += " The built-in list has ended – update backend/data/us_schedule.py."
        else:
            stale = f"Showing the last good copy – latest download failed: {error}" if error else None
        return {
            "source": source,
            "fallback": fallback,
            "fetched_at_utc": iso(datetime.fromtimestamp(fetched_at, tz=timezone.utc)) if fetched_at else None,
            "fetched_at_ist": ist_label(datetime.fromtimestamp(fetched_at, tz=timezone.utc)) if fetched_at else None,
            "stale_note": stale,
            "warning": warning,
            "rules": f"NO TRADE from {BLOCK_BEFORE_MIN} min before to {BLOCK_AFTER_MIN} min after a high-impact USD event; "
                     f"CAUTION within {CAUTION_BEFORE_MIN // 60} h before one.",
            "events": rows,
        }
