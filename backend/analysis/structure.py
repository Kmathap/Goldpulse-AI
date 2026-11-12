"""Market structure from real candles: swing points, trend structure, support/resistance, breakouts.

Everything here is derived from closed candles only. A swing point needs
`right` candles after it to be confirmed, so the newest few candles can never
be swing points (no repainting).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Swing:
    kind: str          # "high" / "low"
    price: float
    time: datetime
    index: int


def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> list[Swing]:
    highs, lows = df["high"].to_numpy(float), df["low"].to_numpy(float)
    out: list[Swing] = []
    for i in range(left, len(df) - right):
        wh = highs[i - left : i + right + 1]
        wl = lows[i - left : i + right + 1]
        if np.argmax(wh) == left and highs[i] > highs[i - 1]:
            out.append(Swing("high", float(highs[i]), df.index[i].to_pydatetime(), i))
        if np.argmin(wl) == left and lows[i] < lows[i - 1]:
            out.append(Swing("low", float(lows[i]), df.index[i].to_pydatetime(), i))
    return out


def structure(swings: list[Swing], close: float | None = None) -> dict:
    """Compare the last two swing highs and the last two swing lows.

    If the latest close is already beyond the swing point that protects the
    trend (below the last higher low in an uptrend, above the last lower high in
    a downtrend), the structure is reported as broken.
    """
    highs = [s for s in swings if s.kind == "high"][-2:]
    lows = [s for s in swings if s.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return {"label": "UNCLEAR", "text": "Not enough confirmed swing points yet.", "score": 0}
    hh, hl = highs[1].price > highs[0].price, lows[1].price > lows[0].price
    if hh and hl:
        label, text, score = "UPTREND", "Higher highs and higher lows", 1
    elif not hh and not hl:
        label, text, score = "DOWNTREND", "Lower highs and lower lows", -1
    elif hh and not hl:
        label, text, score = "MIXED", "Higher high but lower low (expanding range)", 0
    else:
        label, text, score = "MIXED", "Lower high but higher low (contracting range)", 0
    if close is not None and label == "UPTREND" and close < lows[1].price:
        label, score = "UPTREND BROKEN", -1
        text = f"Close {close:.2f} is below the last higher low {lows[1].price:.2f} – uptrend structure broken"
    elif close is not None and label == "DOWNTREND" and close > highs[1].price:
        label, score = "DOWNTREND BROKEN", 1
        text = f"Close {close:.2f} is above the last lower high {highs[1].price:.2f} – downtrend structure broken"
    return {
        "label": label,
        "text": text,
        "score": score,
        "last_swing_high": round(highs[1].price, 2),
        "prev_swing_high": round(highs[0].price, 2),
        "last_swing_low": round(lows[1].price, 2),
        "prev_swing_low": round(lows[0].price, 2),
        "last_swing_high_time": highs[1].time,
        "last_swing_low_time": lows[1].time,
    }


def levels(swings: list[Swing], price: float, atr: float, max_each: int = 3) -> dict:
    """Cluster swing prices that sit within 0.5×ATR of each other into support/resistance levels."""
    if not swings or not atr or atr <= 0:
        return {"supports": [], "resistances": []}
    pts = sorted(swings, key=lambda s: s.price)
    tol = 0.5 * atr
    clusters: list[list[Swing]] = [[pts[0]]]
    for s in pts[1:]:
        if s.price - clusters[-1][-1].price <= tol:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    lv = []
    for c in clusters:
        lv.append(
            {
                "level": round(float(np.mean([s.price for s in c])), 2),
                "touches": len(c),
                "last_touch": max(s.time for s in c),
                "zone_low": round(min(s.price for s in c), 2),
                "zone_high": round(max(s.price for s in c), 2),
            }
        )
    sup = sorted([x for x in lv if x["level"] < price], key=lambda x: -x["level"])[:max_each]
    res = sorted([x for x in lv if x["level"] > price], key=lambda x: x["level"])[:max_each]
    return {"supports": sup, "resistances": res}


def breakout_check(df: pd.DataFrame, level_list: list[dict], atr: float, lookback: int = 5) -> dict | None:
    """Did the latest closed candles break a level, or poke through and fail (false breakout)?"""
    if len(df) < lookback + 2 or not atr:
        return None
    buf = 0.1 * atr
    closes, highs, lows = df["close"].to_numpy(float), df["high"].to_numpy(float), df["low"].to_numpy(float)
    last, before = closes[-1], closes[-lookback - 1 : -1]
    events = []
    for lv in level_list:
        L = lv["level"]
        if (before < L).all() and last > L + buf:
            events.append(("BREAKOUT_UP", L, f"Closed above {L:.2f} after {lookback} closes below it"))
        elif (before > L).all() and last < L - buf:
            events.append(("BREAKDOWN", L, f"Closed below {L:.2f} after {lookback} closes above it"))
        elif (highs[-3:] > L + buf).any() and last < L and closes[-4] < L:
            events.append(("FALSE_BREAKOUT_UP", L, f"Pushed above {L:.2f} but closed back below it (failed breakout)"))
        elif (lows[-3:] < L - buf).any() and last > L and closes[-4] > L:
            events.append(("FALSE_BREAKDOWN", L, f"Dropped below {L:.2f} but closed back above it (failed breakdown)"))
    if not events:
        return None
    kind, L, text = min(events, key=lambda e: abs(e[1] - last))
    return {"type": kind, "level": round(L, 2), "text": text}
