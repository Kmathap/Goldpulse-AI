"""Shared types for all market-data providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import pandas as pd

TIMEFRAMES: tuple[str, ...] = ("M5", "M15", "M30", "H1", "H4", "D1")

TIMEFRAME_SECONDS: dict[str, int] = {
    "M5": 5 * 60,
    "M15": 15 * 60,
    "M30": 30 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
}

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataUnavailableError(Exception):
    """Raised whenever real data cannot be obtained. Never turned into fake data."""


@dataclass(frozen=True)
class Quote:
    price: float
    time_utc: datetime            # when the source says the price was valid
    source: str                   # e.g. "xaus.com spot"
    feed_type: str                # e.g. "indicative mid-market reference rate"
    source_state: str | None = None   # provider's own freshness flag, if any
    bid: float | None = None
    ask: float | None = None


@dataclass(frozen=True)
class CandleSet:
    timeframe: str
    df: pd.DataFrame              # UTC-indexed, CANDLE_COLUMNS, oldest first, may include forming candle
    source: str
    notes: tuple[str, ...] = ()
    last_point_utc: datetime | None = None   # newest raw data point the source sent (to measure delay)


@dataclass(frozen=True)
class ProviderInfo:
    key: str                      # "xaus" / "twelvedata"
    name: str
    symbol: str
    cost: str
    notes: tuple[str, ...] = field(default_factory=tuple)


class MarketDataProvider(Protocol):
    def info(self) -> ProviderInfo: ...

    def get_quote(self) -> Quote: ...

    def get_candles(self, timeframe: str) -> CandleSet: ...

    def shutdown(self) -> None: ...
