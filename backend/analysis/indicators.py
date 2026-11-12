"""Technical indicators written in pandas/NumPy (no TA-Lib dependency).

All functions take real OHLC data and return pandas Series aligned to the input
index. Values are NaN until enough history exists – they are never back-filled.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EMA200_MIN_BARS = 250  # require warm-up beyond the 200-bar seed before trusting EMA200


def _seeded_recursive(values: pd.Series, period: int, alpha: float) -> pd.Series:
    """Exponential smoothing seeded with the simple mean of the first `period` valid values.

    alpha = 2/(n+1) gives a standard EMA; alpha = 1/n gives Wilder's smoothing (RMA).
    Leading NaNs are skipped; NaNs after the seed are not expected (OHLC data).
    """
    arr = values.to_numpy(dtype=float)
    out = np.full(arr.shape[0], np.nan)
    valid = np.flatnonzero(~np.isnan(arr))
    if valid.size == 0:
        return pd.Series(out, index=values.index)
    start = valid[0]
    seed_idx = start + period - 1
    if seed_idx >= arr.shape[0]:
        return pd.Series(out, index=values.index)
    out[seed_idx] = arr[start : seed_idx + 1].mean()
    for i in range(seed_idx + 1, arr.shape[0]):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=values.index)


def ema(close: pd.Series, period: int) -> pd.Series:
    return _seeded_recursive(close, period, 2.0 / (period + 1))


def rma(values: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average."""
    return _seeded_recursive(values, period, 1.0 / period)


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    # no losses at all -> RSI 100; no gains and no losses -> 50
    out = out.where(avg_loss != 0, np.where(avg_gain > 0, 100.0, 50.0))
    return out.where(avg_gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), period)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan
    tr = true_range(high, low, close)
    tr.iloc[0] = np.nan

    tr_s = rma(tr, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, period) / tr_s
        minus_di = 100.0 * rma(minus_dm, period) / tr_s
        di_sum = plus_di + minus_di
        dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    dx = dx.where(di_sum != 0, 0.0).where(plus_di.notna())
    return pd.DataFrame({"adx": rma(dx, period), "plus_di": plus_di, "minus_di": minus_di})


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + num_std * std, mid - num_std * std
    with np.errstate(divide="ignore", invalid="ignore"):
        width_pct = 100.0 * (upper - lower) / mid
        pct_b = (close - lower) / (upper - lower)
    return pd.DataFrame(
        {"bb_upper": upper, "bb_mid": mid, "bb_lower": lower, "bb_width_pct": width_pct, "bb_pct_b": pct_b}
    )


def compute_all(candles: pd.DataFrame) -> pd.DataFrame:
    """Add every Phase-1 indicator as columns. Input must be CLOSED candles, oldest first."""
    df = candles.copy()
    c, h, l = df["close"], df["high"], df["low"]
    df["ema20"] = ema(c, 20)
    df["ema50"] = ema(c, 50)
    df["ema200"] = ema(c, 200)
    if len(df) < EMA200_MIN_BARS:
        df["ema200"] = np.nan
    df["rsi14"] = rsi(c, 14)
    df = df.join(macd(c))
    df["atr14"] = atr(h, l, c, 14)
    df = df.join(adx(h, l, c, 14))
    df = df.join(bollinger(c, 20, 2.0))
    return df


def volatility_regime(atr_series: pd.Series, lookback: int = 100) -> dict:
    """Compare the latest ATR with its own recent median (evidence-based, no fixed price thresholds)."""
    clean = atr_series.dropna()
    if len(clean) < 20:
        return {"regime": None, "ratio_to_median": None, "lookback": 0}
    window = clean.iloc[-lookback:]
    median = float(window.median())
    latest = float(clean.iloc[-1])
    ratio = latest / median if median > 0 else None
    if ratio is None:
        regime = None
    elif ratio < 0.8:
        regime = "LOW"
    elif ratio > 1.25:
        regime = "HIGH"
    else:
        regime = "NORMAL"
    return {"regime": regime, "ratio_to_median": ratio, "lookback": len(window)}
