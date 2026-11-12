"""Strategy lab: compare several versions of the trade rules fairly (walk-forward).

1. The trend engine is evaluated ONCE at every H1 close using only data known
   at that moment (same no-look-ahead rules as backend/backtest.py).
2. Each strategy version turns those snapshots into trades on the same candles.
3. The best version is picked using the FIRST half of the period only.
4. Its result on the SECOND half (never used for choosing) is the honest number.

Entry modes
* "limit":   limit order at the middle of the entry zone (current rule)
* "confirm": wait for price to touch the zone, then for an H1 candle that CLOSES
             in the trade direction inside/near the zone; enter at that close.

Exits: stop-loss or final target; both inside one candle = loss (conservative);
close after MAX_HOLD_BARS. Costs are subtracted from every trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backend.analysis import indicators as ind
from backend.analysis import trend_engine as te
from backend.backtest import COST_PER_OZ, H1_WINDOW, MAX_HOLD_BARS, ORDER_VALID_BARS, WARMUP_H1
from backend.data.candles import resample_h4


@dataclass(frozen=True)
class Variant:
    key: str
    name: str
    entry: str = "limit"
    params: dict = field(default_factory=dict)


WIDE = {"stop_buffer_atr": 1.0, "min_stop_atr": 1.5, "max_risk_atr": 3.5, "target_r": 1.5}
RSI = {"rsi_filter": 35}

VARIANTS: list[Variant] = [
    Variant("CUR", "Current rules"),
    Variant("A", "A – confirmation entry", "confirm"),
    Variant("B", "B – wide stop, 1.5R target", "limit", WIDE),
    Variant("C", "C – skip stretched RSI", "limit", RSI),
    Variant("AB", "A+B", "confirm", WIDE),
    Variant("AC", "A+C", "confirm", RSI),
    Variant("BC", "B+C", "limit", {**WIDE, **RSI}),
    Variant("ABC", "A+B+C", "confirm", {**WIDE, **RSI}),
]


ACTIVE_KEY = "A"      # strategy used by the live dashboard and the signal journal


def get_variant(key: str = ACTIVE_KEY) -> Variant:
    return next(v for v in VARIANTS if v.key == key)


def live_params(key: str = ACTIVE_KEY) -> dict:
    """Parameters for the live trade plan (adds the entry mode so the wording matches)."""
    v = get_variant(key)
    return {**v.params, "entry_mode": v.entry}


# ------------------------------------------------------------------ snapshots
def snapshot_at(h1: pd.DataFrame, d1: pd.DataFrame, i: int) -> dict:
    """Engine state at the close of H1 candle i, using only data known at that moment."""
    t = h1.index[i]
    now = t + pd.Timedelta(hours=1)
    h1_hist = h1.iloc[max(0, i + 1 - H1_WINDOW): i + 1]
    h4 = resample_h4(h1.iloc[: i + 1])
    h4 = h4[h4.index + pd.Timedelta(hours=4) <= now]
    d1c = d1[d1.index + pd.Timedelta(days=1) <= now]
    tfs = {"H1": te.evaluate_timeframe("H1", ind.compute_all(h1_hist))}
    if len(h4) >= 60:
        tfs["H4"] = te.evaluate_timeframe("H4", ind.compute_all(h4))
    if len(d1c) >= 60:
        tfs["D1"] = te.evaluate_timeframe("D1", ind.compute_all(d1c))
    return {"verdict": te.combine(tfs), "h1": tfs["H1"], "close": float(h1["close"].iloc[i])}


def build_snapshots(h1: pd.DataFrame, d1: pd.DataFrame, progress=None) -> list[dict | None]:
    """Engine state at the close of every H1 candle (index-aligned with h1). None during warm-up."""
    snaps: list[dict | None] = [None] * len(h1)
    for i in range(WARMUP_H1, len(h1)):
        snaps[i] = snapshot_at(h1, d1, i)
        if progress and (i - WARMUP_H1) % 200 == 0:
            progress(i, len(h1))
    return snaps


# ------------------------------------------------------------------ simulation
def simulate(h1: pd.DataFrame, snaps: list[dict | None], v: Variant, cost: float = COST_PER_OZ,
             state_out: dict | None = None) -> list[dict]:
    trades: list[dict] = []
    pending: dict | None = None
    open_t: dict | None = None
    hi, lo, op, cl = (h1[c].to_numpy(float) for c in ("high", "low", "open", "close"))

    def close_trade(o, i, px, result):
        risk = abs(o["entry"] - o["stop"])
        pnl = (px - o["entry"]) * (1 if o["side"] == "BUY" else -1) - cost
        trades.append({"variant": v.key, "side": o["side"], "signal_i": o["signal_i"], "signal_time": h1.index[o["signal_i"]],
                       "exit_time": h1.index[i], "entry": round(o["entry"], 2), "stop": round(o["stop"], 2),
                       "target": round(o["target"], 2), "result": result, "r": round(pnl / risk, 3)})

    for i in range(WARMUP_H1, len(h1)):
        # 1) manage open trade
        if open_t is not None:
            o = open_t
            sell = o["side"] == "SELL"
            hit_sl = hi[i] >= o["stop"] if sell else lo[i] <= o["stop"]
            hit_tp = lo[i] <= o["target"] if sell else hi[i] >= o["target"]
            if hit_sl:
                close_trade(o, i, o["stop"], "LOSS"); open_t = None
            elif hit_tp:
                close_trade(o, i, o["target"], "WIN"); open_t = None
            elif i - o["fill_i"] >= MAX_HOLD_BARS:
                close_trade(o, i, cl[i], "TIMEOUT"); open_t = None
            continue

        # 2) pending order
        if pending is not None:
            p = pending
            sell = p["side"] == "SELL"
            if v.entry == "limit":
                if (hi[i] >= p["entry"]) if sell else (lo[i] <= p["entry"]):
                    o = {**p, "fill_i": i}
                    if (hi[i] >= p["stop"]) if sell else (lo[i] <= p["stop"]):
                        close_trade(o, i, p["stop"], "LOSS")
                    else:
                        open_t = o
                    pending = None
                    continue
            else:  # confirm
                if (hi[i] >= p["stop"]) if sell else (lo[i] <= p["stop"]):
                    pending = None          # structure broke before confirmation – no trade
                elif not p["touched"]:
                    p["touched"] = (hi[i] >= p["zone"][0]) if sell else (lo[i] <= p["zone"][1])
                if pending is not None and p["touched"]:
                    rejected = (cl[i] < op[i] and cl[i] <= p["zone"][1]) if sell else (cl[i] > op[i] and cl[i] >= p["zone"][0])
                    if rejected:
                        entry = cl[i]
                        risk = (p["stop"] - entry) if sell else (entry - p["stop"])
                        if 0 < risk <= p["max_risk"]:
                            target = entry - p["target_r"] * risk if sell else entry + p["target_r"] * risk
                            open_t = {**p, "entry": entry, "target": target, "fill_i": i}
                        pending = None
                        continue
            if pending is not None and i - p["signal_i"] >= ORDER_VALID_BARS:
                pending = None
            elif pending is not None:
                continue

        # 3) new signal at this close
        s = snaps[i]
        if s is None:
            continue
        plan = te.trade_plan(s["verdict"], s["h1"], s["close"], "LIVE", basis=0.0, chart_delay_min=0, params=v.params)
        if plan["action"] == "NO TRADE":
            continue
        st = plan["setup"]
        P = {**te.DEFAULT_PARAMS, **v.params}
        pending = {"side": plan["action"], "signal_i": i, "entry": st["entry_mid"], "stop": st["stop_loss"],
                   "target": st["take_profit_2"], "zone": st["entry_zone"], "touched": False,
                   "target_r": P["target_r"], "max_risk": P["max_risk_atr"] * s["h1"]["atr"]}
    if state_out is not None:
        state_out["pending"] = pending
        state_out["open"] = open_t
        state_out["last_i"] = len(h1) - 1
    return trades


# ------------------------------------------------------------------ statistics
def stats(trades: list[dict]) -> dict:
    rs = np.array([t["r"] for t in trades], dtype=float)
    if not len(rs):
        return {"trades": 0, "win_rate": None, "avg_r": None, "total_r": 0.0, "pf": None, "max_dd_r": 0.0, "streak": 0}
    wins, losses = rs[rs > 0], rs[rs <= 0]
    eq = np.cumsum(rs)
    dd = float((np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq).max())
    streak = mx = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        mx = max(mx, streak)
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf")
    return {"trades": int(len(rs)), "win_rate": round(100 * float((rs > 0).mean()), 1), "avg_r": round(float(rs.mean()), 3),
            "total_r": round(float(rs.sum()), 2), "pf": round(pf, 2) if pf != float("inf") else 99.0,
            "max_dd_r": round(dd, 2), "streak": mx}


MIN_TRADES_TO_CHOOSE = 5


def walk_forward(h1: pd.DataFrame, d1: pd.DataFrame, progress=None) -> dict:
    snaps = build_snapshots(h1, d1, progress)
    split_i = WARMUP_H1 + (len(h1) - WARMUP_H1) // 2
    split_time = h1.index[split_i]
    rows = []
    all_trades = []
    for v in VARIANTS:
        tr = simulate(h1, snaps, v)
        all_trades += tr
        first = [t for t in tr if t["signal_i"] < split_i]
        second = [t for t in tr if t["signal_i"] >= split_i]
        rows.append({"key": v.key, "name": v.name, "first": stats(first), "second": stats(second), "all": stats(tr)})

    eligible = [r for r in rows if r["first"]["trades"] >= MIN_TRADES_TO_CHOOSE and r["first"]["avg_r"] is not None]
    chosen = max(eligible, key=lambda r: r["first"]["avg_r"]) if eligible else None
    verdict = judge(chosen)
    return {"period_start": h1.index[WARMUP_H1].isoformat(), "split": split_time.isoformat(), "period_end": h1.index[-1].isoformat(),
            "rows": rows, "chosen": chosen["key"] if chosen else None, "verdict": verdict, "trades": all_trades}


def judge(chosen: dict | None) -> dict:
    if chosen is None:
        return {"state": "NO_CANDIDATE", "text": "No version had enough trades in the first half to choose from."}
    s = chosen["second"]
    if s["trades"] < 10:
        return {"state": "TOO_FEW", "text": f"{chosen['name']} won the first half, but made only {s['trades']} trades in the unseen "
                "second half – too few to trust. Keep testing (signal journal / more history)."}
    if s["avg_r"] is not None and s["avg_r"] >= 0.15 and s["pf"] >= 1.3:
        return {"state": "PROMISING", "text": f"{chosen['name']} also made money on the unseen half "
                f"({s['avg_r']:+.2f} R per trade over {s['trades']} trades, profit factor {s['pf']}). Promising – next step is a "
                "live signal journal, still without real money."}
    if s["avg_r"] is not None and s["avg_r"] > 0:
        return {"state": "WEAK", "text": f"{chosen['name']} was only slightly positive on the unseen half ({s['avg_r']:+.2f} R) – "
                "not strong enough to trade."}
    return {"state": "FAILED", "text": f"{chosen['name']} looked best on the first half but LOST on the unseen half "
            f"({s['avg_r']:+.2f} R per trade). None of these versions is proven – do not trade them with real money."}
