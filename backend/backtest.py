"""Backtest of the GoldPulse trend engine + trade plan on historical candles.

How it works (no look-ahead):
* At the close of every H1 candle the engine sees ONLY candles that had closed
  by then: H1, H4 built from those H1 candles, and D1 candles that had closed.
* If the plan says BUY or SELL, a limit order is placed at the middle of the
  entry zone. It stays valid for ORDER_VALID_BARS H1 candles.
* Once filled, the trade exits at the stop-loss or at TP2 (2R). If both are
  touched inside the same candle, the LOSS is assumed (conservative).
  A trade still open after MAX_HOLD_BARS closes at that candle's close.
* Costs (spread + commission + slippage) are subtracted from every trade.
* One trade at a time.

Not modelled (be aware): news filter (no historical news), the M15 rejection
confirmation (entry is the zone midpoint), and the chart-vs-spot gap (the
series used here is the free chart series).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from backend.analysis import indicators as ind
from backend.analysis import trend_engine as te
from backend.data.candles import resample_h4

ORDER_VALID_BARS = 12        # limit order lives 12 H1 candles
MAX_HOLD_BARS = 72           # close a trade after 3 days
WARMUP_H1 = 250              # H1 candles needed before the first signal (EMA 200 warm-up)
H1_WINDOW = 700             # H1 candles fed to the engine at each step
COST_PER_OZ = 0.40           # USD: spread + commission + slippage, conservative for gold


@dataclass
class Trade:
    side: str
    signal_time: datetime
    fill_time: datetime | None
    exit_time: datetime | None
    entry: float
    stop: float
    target: float
    tp1: float | None
    exit_price: float | None
    result: str              # WIN / LOSS / TIMEOUT / NOT_FILLED
    r_multiple: float | None
    reached_tp1_first: bool | None
    confidence: int | None


def _closed_d1(d1: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    return d1[d1.index + pd.Timedelta(days=1) <= now]


def run_backtest(h1: pd.DataFrame, d1: pd.DataFrame, cost_per_oz: float = COST_PER_OZ,
                 progress: bool = False) -> dict:
    h1 = h1.sort_index()
    trades: list[Trade] = []
    blocked: dict[str, int] = {}
    pending: dict | None = None
    open_t: dict | None = None
    n = len(h1)

    for i in range(WARMUP_H1, n):
        bar = h1.iloc[i]
        t = h1.index[i]

        # ---- 1) manage the open trade on this candle -------------------
        if open_t is not None:
            o = open_t
            hit_sl = bar["high"] >= o["stop"] if o["side"] == "SELL" else bar["low"] <= o["stop"]
            hit_tp = bar["low"] <= o["target"] if o["side"] == "SELL" else bar["high"] >= o["target"]
            if o["tp1"] is not None and o["tp1_first"] is None:
                hit_tp1 = bar["low"] <= o["tp1"] if o["side"] == "SELL" else bar["high"] >= o["tp1"]
                if hit_tp1 and not hit_sl:
                    o["tp1_first"] = True
                elif hit_sl:
                    o["tp1_first"] = False
            exit_px = result = None
            if hit_sl:
                exit_px, result = o["stop"], "LOSS"
            elif hit_tp:
                exit_px, result = o["target"], "WIN"
            elif i - o["fill_i"] >= MAX_HOLD_BARS:
                exit_px, result = float(bar["close"]), "TIMEOUT"
            if exit_px is not None:
                risk = abs(o["entry"] - o["stop"])
                pnl = (exit_px - o["entry"]) * (1 if o["side"] == "BUY" else -1) - cost_per_oz
                trades.append(Trade(o["side"], o["signal_time"], o["fill_time"], t.to_pydatetime(), round(o["entry"], 2),
                                    round(o["stop"], 2), round(o["target"], 2), o["tp1"], round(exit_px, 2), result,
                                    round(pnl / risk, 3), o["tp1_first"], o["conf"]))
                open_t = None
            continue   # one trade at a time; no new signals while in a trade

        # ---- 2) try to fill the pending limit order -----------------------
        if pending is not None:
            p = pending
            touched = bar["high"] >= p["entry"] if p["side"] == "SELL" else bar["low"] <= p["entry"]
            if touched:
                open_t = {**p, "fill_i": i, "fill_time": t.to_pydatetime(), "tp1_first": None}
                pending = None
                # stop hit inside the fill candle -> immediate loss (conservative)
                if (bar["high"] >= p["stop"]) if p["side"] == "SELL" else (bar["low"] <= p["stop"]):
                    risk = abs(p["entry"] - p["stop"])
                    trades.append(Trade(p["side"], p["signal_time"], t.to_pydatetime(), t.to_pydatetime(), round(p["entry"], 2),
                                        round(p["stop"], 2), round(p["target"], 2), p["tp1"], round(p["stop"], 2), "LOSS",
                                        round((-risk - cost_per_oz) / risk, 3), False, p["conf"]))
                    open_t = None
                continue
            if i - p["signal_i"] >= ORDER_VALID_BARS:
                trades.append(Trade(p["side"], p["signal_time"], None, None, round(p["entry"], 2), round(p["stop"], 2),
                                    round(p["target"], 2), p["tp1"], None, "NOT_FILLED", None, None, p["conf"]))
                pending = None
            else:
                continue

        # ---- 3) evaluate the engine at this candle's close ------------------
        h1_hist = h1.iloc[max(0, i + 1 - H1_WINDOW): i + 1]     # recent window keeps it fast; EMAs fully warmed
        h4_hist = resample_h4(h1.iloc[: i + 1])
        h4_closed = h4_hist[h4_hist.index + pd.Timedelta(hours=4) <= t + pd.Timedelta(hours=1)]
        d1_closed = _closed_d1(d1, t + pd.Timedelta(hours=1))
        tfs = {"H1": te.evaluate_timeframe("H1", ind.compute_all(h1_hist))}
        if len(h4_closed) >= 60:
            tfs["H4"] = te.evaluate_timeframe("H4", ind.compute_all(h4_closed))
        if len(d1_closed) >= 60:
            tfs["D1"] = te.evaluate_timeframe("D1", ind.compute_all(d1_closed))
        verdict = te.combine(tfs)
        plan = te.trade_plan(verdict, tfs["H1"], float(bar["close"]), "LIVE", basis=0.0, chart_delay_min=0)
        if plan["action"] == "NO TRADE":
            r = plan["reason"]
            key = ("Confidence below 60/100" if r.startswith("Evidence agreement")
                   else "Poor reward-to-risk (first target under 1R)" if r.startswith("Poor reward")
                   else "Stop would be too wide (over 2.5× ATR)" if "too wide" in r
                   else "H1 trend too weak (ADX under 18)" if "too weak" in r
                   else re.sub(r"\d+(\.\d+)?", "N", r.split(" – ")[0])[:70])
            blocked[key] = blocked.get(key, 0) + 1
            continue
        s = plan["setup"]
        pending = {"side": plan["action"], "signal_i": i, "signal_time": t.to_pydatetime(), "entry": s["entry_mid"],
                   "stop": s["stop_loss"], "target": s["take_profit_2"], "tp1": s["take_profit_1"],
                   "conf": verdict["confidence"]}
        if progress and i % 100 == 0:
            print(f"  … {i}/{n} candles")

    return summarise(trades, blocked, h1)


def summarise(trades: list[Trade], blocked: dict[str, int], h1: pd.DataFrame) -> dict:
    done = [t for t in trades if t.result in ("WIN", "LOSS", "TIMEOUT")]
    rs = np.array([t.r_multiple for t in done], dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    eq_r = np.cumsum(rs) if len(rs) else np.array([0.0])
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq_r]))
    dd_r = float((peak[1:] - eq_r).max()) if len(rs) else 0.0
    streak = mx = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        mx = max(mx, streak)
    # equity at 1 % risk per trade, compounding
    eq = 1.0
    curve = [1.0]
    for r in rs:
        eq *= 1 + 0.01 * r
        curve.append(eq)
    curve = np.array(curve)
    dd_pct = float(((np.maximum.accumulate(curve) - curve) / np.maximum.accumulate(curve)).max() * 100)
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else (float("inf") if len(wins) else 0.0)
    tp1 = [t for t in done if t.tp1 is not None]
    return {
        "period_start": h1.index[WARMUP_H1].isoformat() if len(h1) > WARMUP_H1 else None,
        "period_end": h1.index[-1].isoformat(),
        "h1_candles_tested": max(0, len(h1) - WARMUP_H1),
        "signals": len(trades),
        "not_filled": sum(1 for t in trades if t.result == "NOT_FILLED"),
        "trades": len(done),
        "wins": int((rs > 0).sum()),
        "losses": int((rs <= 0).sum()),
        "timeouts": sum(1 for t in done if t.result == "TIMEOUT"),
        "win_rate_pct": round(100 * float((rs > 0).mean()), 1) if len(rs) else None,
        "avg_r": round(float(rs.mean()), 3) if len(rs) else None,
        "total_r": round(float(rs.sum()), 2) if len(rs) else 0.0,
        "profit_factor": round(pf, 2) if pf != float("inf") else "no losses",
        "max_losing_streak": mx,
        "max_drawdown_r": round(dd_r, 2),
        "return_pct_at_1pct_risk": round(float(curve[-1] - 1) * 100, 2),
        "max_drawdown_pct_at_1pct_risk": round(dd_pct, 2),
        "tp1_reached_before_stop_pct": round(100 * sum(1 for t in tp1 if t.reached_tp1_first) / len(tp1), 1) if tp1 else None,
        "blocked_signals": dict(sorted(blocked.items(), key=lambda kv: -kv[1])),
        "trade_list": [asdict(t) for t in trades],
    }


def verdict_on_results(s: dict) -> list[str]:
    """Plain-language reading of the numbers (minimums are deliberately strict)."""
    notes = []
    n = s["trades"]
    if n < 30:
        notes.append(f"Only {n} completed trades – too few to judge (aim for 30+). Treat any result as unproven.")
    if s["avg_r"] is not None:
        if s["avg_r"] > 0.15 and (s["profit_factor"] == "no losses" or s["profit_factor"] >= 1.3):
            notes.append("Positive expectancy in this sample.")
        elif s["avg_r"] > 0:
            notes.append("Slightly positive but thin – costs or slippage could erase it.")
        else:
            notes.append("Negative expectancy – these rules lost money in this period. Do not trade them live.")
    if s["max_losing_streak"] >= 6:
        notes.append(f"Expect losing streaks of {s['max_losing_streak']}+ trades.")
    return notes


# ---------------------------------------------------------------- status for the dashboard
PASS_MIN_TRADES = 30
PASS_MIN_AVG_R = 0.15
PASS_MIN_PF = 1.3


def latest_status(folder) -> dict:
    """Read the newest summary_*.json and say plainly whether the rules have passed."""
    import json
    from pathlib import Path

    lab = sorted(Path(folder).glob("lab_summary_*.json"))
    if lab:
        from backend.strategy_lab import ACTIVE_KEY
        try:
            L = json.loads(lab[-1].read_text(encoding="utf-8"))
            row = next(r for r in L["rows"] if r["key"] == ACTIVE_KEY)
        except (OSError, ValueError, StopIteration, KeyError):
            row = None
        if row:
            a, sec = row["all"], row["second"]
            passed = (a["trades"] >= PASS_MIN_TRADES and (a["avg_r"] or 0) >= PASS_MIN_AVG_R and (a["pf"] or 0) >= PASS_MIN_PF
                      and (sec["avg_r"] or 0) > 0)
            if passed:
                state = "PASSED"
                msg = f"Strategy {row['name']}: {a['avg_r']:+.2f} R per trade over {a['trades']} trades – passed the backtest."
            elif (a["avg_r"] or 0) <= 0:
                state = "UNPROVEN"
                msg = (f"Strategy {row['name']}: backtest {a['avg_r']:+.2f} R per trade over {a['trades']} trades "
                       f"(unseen half {sec['avg_r']:+.2f} R over {sec['trades']}). Not proven profitable – for study only, "
                       f"no real money. The signal journal is collecting live evidence.")
            else:
                state = "UNPROVEN"
                msg = (f"Strategy {row['name']}: {a['avg_r']:+.2f} R per trade over {a['trades']} trades – too few to trust. "
                       "For study only; the signal journal is collecting live evidence.")
            return {"state": state, "message": msg, "file": lab[-1].name, "period_start": L.get("period_start"),
                    "period_end": L.get("period_end"), "trades": a["trades"], "avg_r": a["avg_r"],
                    "win_rate_pct": a["win_rate"], "profit_factor": a["pf"]}
    files = sorted(Path(folder).glob("summary_*.json"))
    if not files:
        return {"state": "NOT_TESTED", "message": "These rules have not been backtested yet – for study only, not live trading."}
    f = files[-1]
    try:
        s = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "UNKNOWN", "message": f"Could not read {f.name}."}
    pf = s.get("profit_factor")
    pf_ok = pf == "no losses" or (isinstance(pf, (int, float)) and pf >= PASS_MIN_PF)
    avg = s.get("avg_r")
    n = s.get("trades", 0)
    passed = n >= PASS_MIN_TRADES and avg is not None and avg >= PASS_MIN_AVG_R and pf_ok
    if passed:
        state, msg = "PASSED", f"Backtest passed: {avg:+.2f} R per trade over {n} trades (profit factor {pf})."
    elif avg is not None and avg <= 0:
        state, msg = "FAILED", (f"Backtest FAILED: these rules lost {avg:+.2f} R per trade over {n} trades "
                                f"(win rate {s.get('win_rate_pct')}%). For study only – do not trade them with real money.")
    else:
        state, msg = "UNPROVEN", (f"Backtest unproven: {avg:+.2f} R per trade over {n} trades – needs at least "
                                  f"{PASS_MIN_TRADES} trades, {PASS_MIN_AVG_R:+.2f} R and profit factor {PASS_MIN_PF}.")
    return {"state": state, "message": msg, "file": f.name, "period_start": s.get("period_start"),
            "period_end": s.get("period_end"), "trades": n, "avg_r": avg, "win_rate_pct": s.get("win_rate_pct"),
            "profit_factor": pf, "return_pct_at_1pct_risk": s.get("return_pct_at_1pct_risk")}
