"""TEST-ONLY fake HTTP responses. Never imported by the application."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from backend.data.base import DataUnavailableError

NOW = datetime(2026, 11, 11, 13, 2, 30, tzinfo=timezone.utc).timestamp()   # Wednesday
STEP = {"5m": 300, "15m": 900, "30m": 1800, "60m": 3600}


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _series(n: int, step: int, last_open: float, base: float = 4100.0):
    opens = last_open - step * np.arange(n)[::-1]
    closes = base + np.linspace(0, 50, n) + np.sin(np.arange(n) / 5) * 5
    return [
        {"t": int(t), "o": round(c - 0.5, 2), "h": round(c + 1.0, 2), "l": round(c - 1.5, 2), "c": round(c, 2), "v": 100}
        for t, c in zip(opens, closes)
    ]


class FakeXaus:
    """Mimics xaus.com /spot and /chart. Counts calls to check caching."""

    def __init__(self, clock: Clock, spot_price: float = 4150.40, chart_shift: float = 0.0):
        self.clock = clock
        self.spot_price = spot_price
        self.spot_age = 20
        self.spot_state = "fresh"
        self.chart_shift = chart_shift
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self.ms_timestamps = False
        self.live_point_lag: float | None = None   # mimic the source's off-boundary 'latest price' point

    def __call__(self, url, params=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint in self.fail:
            raise DataUnavailableError(f"fake failure for {endpoint}")
        if endpoint == "spot":
            as_of = datetime.fromtimestamp(self.clock() - self.spot_age, tz=timezone.utc).isoformat()
            return {"spot_usd_oz": self.spot_price, "xau": {"price": self.spot_price},
                    "data_state": {"status": self.spot_state, "as_of": as_of, "source": "upstream"},
                    "updated_at": as_of, "source": "xaus.com"}
        if endpoint == "chart":
            interval = params["interval"]
            now = self.clock()
            if interval == "1d":
                last_open = datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc).timestamp()
                pts = _series(500, 86400, last_open)
            else:
                step = STEP[interval]
                pts = _series(600, step, now - (now % step))
            # make the newest close match spot + shift
            pts[-1]["c"] = round(self.spot_price + self.chart_shift, 2)
            pts[-1]["h"] = max(pts[-1]["h"], pts[-1]["c"])
            pts[-1]["l"] = min(pts[-1]["l"], pts[-1]["c"])
            if self.live_point_lag is not None and interval != "1d":
                c = round(self.spot_price + self.chart_shift, 2)
                live_t = int(now - self.live_point_lag)
                pts = [p for p in pts if p["t"] <= live_t]      # the real feed has nothing newer
                pts.append({"t": live_t, "o": c, "h": c, "l": c, "c": c, "v": 0})
            if self.ms_timestamps:
                for p in pts:
                    p["t"] *= 1000
            return {"symbol": "xau", "interval": interval, "points": pts}
        raise AssertionError(f"unexpected url {url}")


class FakeTwelveData:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.calls: list[tuple[str, dict]] = []
        self.error: dict | None = None

    def __call__(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if self.error:
            return self.error
        endpoint = url.rsplit("/", 1)[-1]
        now = self.clock()
        if endpoint == "quote":
            return {"symbol": "XAU/USD", "close": "4150.40", "timestamp": int(now) - 60,
                    "last_quote_at": int(now) - 5, "is_market_open": True}
        if endpoint == "time_series":
            step = {"5min": 300, "15min": 900, "30min": 1800, "1h": 3600, "4h": 14400, "1day": 86400}[params["interval"]]
            last = now - (now % step)
            vals = []
            for p in _series(600, step, last):
                dt = datetime.fromtimestamp(p["t"], tz=timezone.utc)
                fmt = "%Y-%m-%d" if step == 86400 else "%Y-%m-%d %H:%M:%S"
                vals.append({"datetime": dt.strftime(fmt), "open": str(p["o"]), "high": str(p["h"]),
                             "low": str(p["l"]), "close": str(p["c"])})
            return {"meta": {"symbol": "XAU/USD"}, "values": vals, "status": "ok"}
        raise AssertionError(url)
