"""Free market-data provider: xaus.com public API (no key, no account).

* Live price:  GET /api/v1/spot   – indicative mid-market spot, cached by xaus.com ~60 s
* Candles:     GET /api/v1/chart  – OHLCV series (5m/15m for 5 days, 30m for 1 month,
                                    60m for 3 months, 1d for 2 years)
* H4 candles are built here from the real H1 candles.

Limits (xaus.com docs): 60 spot requests and 20 chart requests per minute per IP;
cache at least 60 s. We cache spot 60 s and chart series 60–300 s.

xaus.com does not state whether the chart series is spot or futures-based, so the
service compares the chart's latest close with the live spot price and reports
the gap ("basis") on every snapshot.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from backend.config import Settings
from backend.data.base import (
    TIMEFRAME_SECONDS,
    TIMEFRAMES,
    CandleSet,
    DataUnavailableError,
    ProviderInfo,
    Quote,
)
from backend.data.candles import TTLCache, align_to_interval, build_frame, parse_time, resample_h4
from backend.data.http import HttpGet, make_http_get

BASE_URL = "https://xaus.com/api/v1"

# our timeframe -> (xaus interval, xaus range, cache seconds)
CHART_PLAN: dict[str, tuple[str, str, int]] = {
    "M5": ("5m", "5d", 60),
    "M15": ("15m", "5d", 90),
    "M30": ("30m", "1mo", 120),
    "H1": ("60m", "3mo", 180),
    "D1": ("1d", "2y", 300),
}
SPOT_TTL = 60
CHART_SOURCE = "xaus.com chart series (OHLC)"
SPOT_SOURCE = "xaus.com spot (indicative mid-market)"


def _extract_points(body: Any) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("points", "data", "candles", "series", "values", "bars"):
            val = body.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):          # e.g. {"data": {"points": [...]}}
                inner = _extract_points(val)
                if inner:
                    return inner
    keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
    raise DataUnavailableError(f"xaus.com chart response has an unexpected format (keys: {keys}).")


class XausProvider:
    def __init__(
        self,
        settings: Settings,
        http_get: HttpGet | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._get = http_get or make_http_get(settings.http_timeout_seconds)
        self._cache = TTLCache(clock)

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            key="xaus",
            name="xaus.com (free public API)",
            symbol="XAU/USD",
            cost="Free – no key",
            notes=(
                "Spot is an indicative mid-market reference rate, not a broker's tradable bid/ask.",
                "xaus.com does not say whether its chart series is spot or futures; the app measures the gap to spot.",
                "H4 candles are built from H1 candles (00:00/04:00/… UTC buckets).",
            ),
        )

    # ------------------------------------------------------------------ #
    def get_quote(self) -> Quote:
        return self._cache.get_or_load("spot", SPOT_TTL, self._load_quote)

    def _load_quote(self) -> Quote:
        body = self._get(f"{BASE_URL}/spot", {"compact": 1})
        if not isinstance(body, dict):
            raise DataUnavailableError("xaus.com spot response has an unexpected format.")
        state = body.get("data_state") or {}
        status = state.get("status")
        if status == "unavailable":
            raise DataUnavailableError("xaus.com reports no live gold price is available right now.")
        price = body.get("spot_usd_oz")
        if price is None and isinstance(body.get("xau"), dict):
            price = body["xau"].get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise DataUnavailableError("xaus.com spot response contained no gold price.") from None
        if price <= 0:
            raise DataUnavailableError(f"xaus.com returned an invalid gold price ({price}).")
        as_of = state.get("as_of") or body.get("updated_at")
        if not as_of:
            raise DataUnavailableError("xaus.com spot response has no timestamp, so its age cannot be checked.")
        return Quote(
            price=price,
            time_utc=parse_time(as_of),
            source=SPOT_SOURCE,
            feed_type="indicative reference rate (refreshed about every 60 s)",
            source_state=status,
        )

    # ------------------------------------------------------------------ #
    def get_candles(self, timeframe: str) -> CandleSet:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {TIMEFRAMES}")
        if timeframe == "H4":
            h1 = self.get_candles("H1")
            return CandleSet(
                "H4", resample_h4(h1.df), CHART_SOURCE, h1.notes + ("H4 built from H1 candles.",), h1.last_point_utc
            )
        interval, rng, ttl = CHART_PLAN[timeframe]
        return self._cache.get_or_load(f"chart:{timeframe}", ttl, lambda: self._load_chart(timeframe, interval, rng))

    def _load_chart(self, timeframe: str, interval: str, rng: str) -> CandleSet:
        body = self._get(f"{BASE_URL}/chart", {"symbol": "xau", "interval": interval, "range": rng})
        df = build_frame(
            _extract_points(body),
            {"time": "t", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"},
            f"xaus.com {timeframe}",
        )
        last_point = df.index[-1].to_pydatetime()
        if timeframe != "D1":   # daily candles follow the source's own session start, keep as-is
            df = align_to_interval(df, TIMEFRAME_SECONDS[timeframe])
        notes: tuple[str, ...] = ()
        if isinstance(body, dict) and body.get("note"):
            notes = (str(body["note"]),)
        return CandleSet(timeframe, df, CHART_SOURCE, notes, last_point)

    def get_history(self, timeframe: str, bars: int = 5000) -> CandleSet:
        """The free feed has no longer history than the live series (about 3 months of H1)."""
        return self.get_candles(timeframe)

    def shutdown(self) -> None:
        pass
