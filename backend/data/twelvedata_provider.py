"""Paid upgrade provider: Twelve Data (XAU/USD needs the Grow plan or higher).

Switched on automatically when TWELVEDATA_API_KEY is set in .env.
The key is sent only from this backend and is masked in every error message.

Endpoints used:
* /quote        – latest price and its timestamp (1 credit)
* /time_series  – OHLC candles for 5min, 15min, 30min, 1h, 4h, 1day (1 credit each)
"""
from __future__ import annotations

import time
from typing import Any, Callable

from backend.config import Settings
from backend.data.base import TIMEFRAMES, CandleSet, DataUnavailableError, ProviderInfo, Quote
from backend.data.candles import TTLCache, build_frame, parse_time
from backend.data.http import HttpGet, make_http_get

BASE_URL = "https://api.twelvedata.com"
SYMBOL = "XAU/USD"
INTERVALS = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1day"}
TTL = {"M5": 30, "M15": 45, "M30": 60, "H1": 90, "H4": 120, "D1": 300}
OUTPUT_SIZE = 600
SOURCE = "Twelve Data XAU/USD"


def _check(body: Any, what: str) -> dict:
    if not isinstance(body, dict):
        raise DataUnavailableError(f"Twelve Data {what}: unexpected response format.")
    if body.get("status") == "error" or ("code" in body and "message" in body and "values" not in body):
        code, msg = body.get("code"), body.get("message", "unknown error")
        hint = " (XAU/USD needs the Grow plan or higher)" if code in (403, 404) else ""
        raise DataUnavailableError(f"Twelve Data {what} error {code}: {msg}{hint}")
    return body


class TwelveDataProvider:
    def __init__(
        self,
        settings: Settings,
        http_get: HttpGet | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not settings.twelvedata_api_key:
            raise ValueError("TwelveDataProvider needs TWELVEDATA_API_KEY")
        self._key = settings.twelvedata_api_key
        self._get = http_get or make_http_get(settings.http_timeout_seconds)
        self._cache = TTLCache(clock)

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            key="twelvedata",
            name="Twelve Data (paid plan)",
            symbol=SYMBOL,
            cost="Grow plan or higher",
            notes=("Candles and quote come from the same XAU/USD spot series.",),
        )

    def get_quote(self) -> Quote:
        return self._cache.get_or_load("quote", 20, self._load_quote)

    def _load_quote(self) -> Quote:
        body = _check(self._get(f"{BASE_URL}/quote", {"symbol": SYMBOL, "apikey": self._key}), "quote")
        try:
            price = float(body["close"])
        except (KeyError, TypeError, ValueError):
            raise DataUnavailableError("Twelve Data quote contained no price.") from None
        stamp = body.get("last_quote_at") or body.get("timestamp")
        if not stamp:
            raise DataUnavailableError("Twelve Data quote has no timestamp, so its age cannot be checked.")
        return Quote(
            price=price,
            time_utc=parse_time(stamp),
            source=SOURCE,
            feed_type="real-time data feed (per plan)",
            source_state="market open" if body.get("is_market_open") else "market closed",
        )

    def get_candles(self, timeframe: str) -> CandleSet:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {TIMEFRAMES}")
        return self._cache.get_or_load(f"ts:{timeframe}", TTL[timeframe], lambda: self._load(timeframe))

    def _load(self, timeframe: str, outputsize: int = OUTPUT_SIZE) -> CandleSet:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVALS[timeframe],
            "outputsize": outputsize,
            "timezone": "UTC",
            "order": "asc",
            "apikey": self._key,
        }
        body = _check(self._get(f"{BASE_URL}/time_series", params), f"{timeframe} candles")
        values = body.get("values")
        if not isinstance(values, list):
            raise DataUnavailableError(f"Twelve Data {timeframe} candles: no 'values' in response.")
        df = build_frame(
            values,
            {"time": "datetime", "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"},
            f"Twelve Data {timeframe}",
        )
        return CandleSet(timeframe, df, SOURCE)

    def get_history(self, timeframe: str, bars: int = 5000) -> CandleSet:
        """Long history for backtests (Twelve Data returns at most 5000 candles per request)."""
        bars = max(100, min(int(bars), 5000))
        return self._cache.get_or_load(f"hist:{timeframe}:{bars}", 3600, lambda: self._load(timeframe, bars))

    def shutdown(self) -> None:
        pass
