"""Helpers shared by providers: timestamp parsing, DataFrame building, H4 resampling, caching."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from backend.data.base import CANDLE_COLUMNS, DataUnavailableError


def parse_time(value: Any) -> datetime:
    """Accept unix seconds, unix milliseconds, ISO strings or 'YYYY-MM-DD[ HH:MM:SS]' (treated as UTC)."""
    if value is None or value == "":
        raise DataUnavailableError("Data point has no timestamp.")
    if isinstance(value, (int, float, np.integer, np.floating)):
        v = float(value)
        if v > 1e12:          # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_time(int(s))
        try:
            ts = pd.Timestamp(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise DataUnavailableError(f"Unrecognised timestamp {value!r}") from None
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").to_pydatetime()
    raise DataUnavailableError(f"Unrecognised timestamp {value!r}")


def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return np.nan
    return f if np.isfinite(f) else np.nan


def build_frame(rows: Iterable[dict[str, Any]], keys: dict[str, str], what: str) -> pd.DataFrame:
    """Build a clean UTC-indexed candle frame.

    keys maps our column names (time/open/high/low/close/volume) to the source's field names.
    Rows with a missing high, low or close are dropped (never filled in).
    """
    records = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        records.append(
            {
                "time": parse_time(r.get(keys["time"])),
                "open": _num(r.get(keys["open"])) if keys.get("open") else np.nan,
                "high": _num(r.get(keys["high"])),
                "low": _num(r.get(keys["low"])),
                "close": _num(r.get(keys["close"])),
                "volume": _num(r.get(keys["volume"])) if keys.get("volume") else np.nan,
            }
        )
    if not records:
        raise DataUnavailableError(f"{what}: the source returned no candles.")
    df = pd.DataFrame.from_records(records).set_index("time")
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC")
    df.index.name = "time_utc"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["high", "low", "close"])
    bad = (df["high"] < df["low"]) | (df["close"] <= 0)
    df = df[~bad]
    if df.empty:
        raise DataUnavailableError(f"{what}: every candle was incomplete or invalid.")
    return df[CANDLE_COLUMNS]


def align_to_interval(df: pd.DataFrame, seconds: int) -> pd.DataFrame:
    """Snap candle times to their interval boundary and merge points that share a bucket.

    Some sources append a "latest price" point with an off-boundary timestamp
    (e.g. 19:34:40 on a 5-minute series). It belongs to the 19:30 candle, so it is
    merged into that candle (high = max, low = min, close = last) – never treated
    as a separate, finished candle.
    """
    if df.empty:
        return df
    floored = df.index.floor(f"{seconds}s")
    if (floored == df.index).all():
        return df
    out = df.groupby(floored).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    out.index.name = df.index.name
    return out[CANDLE_COLUMNS]


def resample_h4(h1: pd.DataFrame) -> pd.DataFrame:
    """Build H4 candles from real H1 candles (buckets start 00:00, 04:00 … UTC)."""
    agg = h1.resample("4h", origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return agg.dropna(subset=["high", "low", "close"])


class TTLCache:
    """Thread-safe cache so we respect each provider's rate limits."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_load(self, key: str, ttl: float, loader: Callable[[], Any]) -> Any:
        now = self._clock()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = loader()
        with self._lock:
            self._data[key] = (self._clock(), value)
        return value
