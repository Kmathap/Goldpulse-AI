"""Rules-based bullish / bearish / neutral engine and educational trade plan.

Design rules (from the project brief):
* Evidence-based: every verdict lists the facts behind it AND the facts against it.
* No forced signal: when timeframes or indicators conflict the verdict is NEUTRAL
  and the plan says NO TRADE – WAIT FOR CONFIRMATION.
* Confidence (0–95) measures how strongly the evidence agrees. It is NOT a
  probability of profit and is never 100.
* Entry, stop and targets come only from real swing levels, EMAs and ATR.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from backend.analysis.structure import breakout_check, find_swings, levels, structure

TF_WEIGHTS = {"D1": 0.20, "H4": 0.25, "H1": 0.25, "M30": 0.10, "M15": 0.12, "M5": 0.08}
COMPONENT_WEIGHTS = {"ema": 0.30, "structure": 0.25, "di": 0.20, "macd": 0.15, "rsi": 0.10}
TF_THRESHOLD = 40          # per-timeframe score needed to call that timeframe bullish/bearish
COMBINED_THRESHOLD = 30    # combined score needed for a directional verdict
MIN_CONFIDENCE_FOR_SETUP = 60
MIN_RR_FIRST_TARGET = 1.0     # first opposite level must give at least 1R

DEFAULT_PARAMS = {
    "stop_buffer_atr": 0.5,     # buffer beyond the zone / level / swing
    "min_stop_atr": 1.0,        # never closer than this to the entry
    "max_risk_atr": 2.5,        # refuse setups with a wider stop
    "target_r": 2.0,            # final target in R
    "rsi_filter": None,         # e.g. 35 -> no sells below RSI 35 / no buys above 65
    "min_confidence": MIN_CONFIDENCE_FOR_SETUP,
    "min_adx": 18,
    "entry_mode": "limit",      # "limit" (zone midpoint) or "confirm" (H1 close in trade direction)
}
LEVEL_LOOKBACK = 200       # candles used to find support/resistance


def _sign(x: float) -> int:
    return 0 if x is None or pd.isna(x) or x == 0 else (1 if x > 0 else -1)


def _r(x: Any, nd: int = 2):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else round(f, nd)


# --------------------------------------------------------------------------- per timeframe
def evaluate_timeframe(tf: str, data: pd.DataFrame) -> dict:
    """`data` = closed candles with indicator columns from indicators.compute_all()."""
    last = data.iloc[-1]
    close = float(last["close"])

    ema_votes = []
    ema_facts = []
    for col, name in (("ema20", "EMA 20"), ("ema50", "EMA 50"), ("ema200", "EMA 200")):
        if not pd.isna(last[col]):
            v = 1 if close > last[col] else -1
            ema_votes.append(v)
            ema_facts.append((name, v))
    if not pd.isna(last["ema20"]) and not pd.isna(last["ema50"]):
        ema_votes.append(1 if last["ema20"] > last["ema50"] else -1)
    ema_c = float(np.mean(ema_votes)) if ema_votes else 0.0

    swings = find_swings(data.iloc[-LEVEL_LOOKBACK:])
    st = structure(swings, close)
    macd_c = 0.5 * _sign(last["macd_hist"]) + 0.5 * _sign(last["macd"])
    rsi = float(last["rsi14"])
    rsi_c = 1 if rsi > 55 else -1 if rsi < 45 else 0
    di_c = _sign(last["plus_di"] - last["minus_di"])

    comps = {"ema": ema_c, "structure": st["score"], "di": di_c, "macd": macd_c, "rsi": rsi_c}
    score = round(100 * sum(COMPONENT_WEIGHTS[k] * v for k, v in comps.items()))
    label = "BULLISH" if score >= TF_THRESHOLD else "BEARISH" if score <= -TF_THRESHOLD else "NEUTRAL"
    adx = float(last["adx"])
    strength = "STRONG" if adx >= 25 else "MODERATE" if adx >= 20 else "WEAK"

    above = [n for n, v in ema_facts if v > 0]
    below = [n for n, v in ema_facts if v < 0]
    atr = float(last["atr14"])
    lv = levels(swings, close, atr)
    brk = breakout_check(data, lv["supports"] + lv["resistances"], atr)

    return {
        "timeframe": tf,
        "score": score,
        "label": label,
        "strength": strength,
        "components": {k: round(float(v), 2) for k, v in comps.items()},
        "close": _r(close),
        "atr": _r(atr),
        "adx": _r(adx, 1),
        "rsi": _r(rsi, 1),
        "macd_hist": _r(last["macd_hist"], 3),
        "plus_di": _r(last["plus_di"], 1),
        "minus_di": _r(last["minus_di"], 1),
        "ema20": _r(last["ema20"]),
        "ema50": _r(last["ema50"]),
        "ema200": _r(last["ema200"]),
        "above_emas": above,
        "below_emas": below,
        "structure": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in st.items()},
        "supports": [{**x, "last_touch": x["last_touch"].isoformat()} for x in lv["supports"]],
        "resistances": [{**x, "last_touch": x["last_touch"].isoformat()} for x in lv["resistances"]],
        "breakout": brk,
    }


def _tf_fact(t: dict, direction: int) -> str:
    """One-line evidence summary for a timeframe, phrased for the given direction."""
    parts = []
    if direction < 0 and t["below_emas"]:
        parts.append("price below " + "/".join(t["below_emas"]))
    if direction > 0 and t["above_emas"]:
        parts.append("price above " + "/".join(t["above_emas"]))
    if t["structure"]["score"] == direction:
        parts.append(t["structure"]["text"].lower())
    if _sign(t["plus_di"] - t["minus_di"]) == direction:
        parts.append(("+DI" if direction > 0 else "−DI") + f" leads (ADX {t['adx']})")
    if _sign(t["macd_hist"]) == direction:
        parts.append(f"MACD histogram {'positive' if direction > 0 else 'negative'}")
    return f"{t['timeframe']}: " + ", ".join(parts) if parts else ""


# --------------------------------------------------------------------------- combined verdict
def combine(tfs: dict[str, dict]) -> dict:
    avail = {k: v for k, v in tfs.items() if v}
    wsum = sum(TF_WEIGHTS[k] for k in avail)
    score = round(sum(TF_WEIGHTS[k] * v["score"] for k, v in avail.items()) / wsum) if wsum else 0
    h1, h4 = avail.get("H1"), avail.get("H4")

    conflict = None
    if score >= COMBINED_THRESHOLD:
        if any(t and t["label"] == "BEARISH" for t in (h1, h4)):
            conflict = "Higher timeframes disagree: H1 or H4 is bearish while the overall score leans bullish."
        direction = "BULLISH" if not conflict else "NEUTRAL"
    elif score <= -COMBINED_THRESHOLD:
        if any(t and t["label"] == "BULLISH" for t in (h1, h4)):
            conflict = "Higher timeframes disagree: H1 or H4 is bullish while the overall score leans bearish."
        direction = "BEARISH" if not conflict else "NEUTRAL"
    else:
        direction = "NEUTRAL"

    d = 1 if direction == "BULLISH" else -1 if direction == "BEARISH" else (1 if score > 0 else -1)
    agree = sum(TF_WEIGHTS[k] for k, v in avail.items() if (v["label"] == "BULLISH") == (d > 0) and v["label"] != "NEUTRAL")
    agreement = agree / wsum if wsum else 0
    adxs = [t["adx"] for t in (h1, h4) if t and t["adx"] is not None]
    avg_adx = float(np.mean(adxs)) if adxs else 0
    strength = "STRONG" if avg_adx >= 25 else "MODERATE" if avg_adx >= 20 else "WEAK"

    if direction == "NEUTRAL":
        confidence = None
        lean = "slightly bearish" if score <= -10 else "slightly bullish" if score >= 10 else "no lean"
    else:
        confidence = int(min(95, round(0.5 * abs(score) + 50 * agreement)))
        lean = None

    reasons_extra: list[str] = []
    reasons = [f for f in (_tf_fact(avail[k], d) for k in ("D1", "H4", "H1", "M30", "M15", "M5") if k in avail) if f]
    against = []
    for k in ("D1", "H4", "H1", "M30", "M15", "M5"):
        t = avail.get(k)
        if not t:
            continue
        if _sign(t["macd_hist"]) == -d:
            against.append(f"{k}: MACD histogram {'positive' if d < 0 else 'negative'} ({t['macd_hist']:+}) – momentum is turning against the {('bearish' if d < 0 else 'bullish')} case")
        if t["structure"]["score"] == -d:
            against.append(f"{k}: structure shows {t['structure']['text'].lower()}")
        if d < 0 and t["rsi"] is not None and t["rsi"] <= 30:
            against.append(f"{k}: RSI {t['rsi']} is oversold – a bounce is common before any continuation")
        if d > 0 and t["rsi"] is not None and t["rsi"] >= 70:
            against.append(f"{k}: RSI {t['rsi']} is overbought – a pullback is common before any continuation")
    for k in ("H4", "H1", "M30", "M15"):
        t = avail.get(k)
        b = t and t.get("breakout")
        if not b:
            continue
        bull = b["type"] in ("BREAKOUT_UP", "FALSE_BREAKDOWN")
        (against if (bull and d < 0) or (not bull and d > 0) else reasons_extra).append(f"{k}: {b['text']}")
    if strength == "WEAK":
        against.append(f"Trend strength is weak (average H1/H4 ADX {avg_adx:.1f} < 20) – moves may not follow through")
    if conflict:
        against.insert(0, conflict)

    return {
        "direction": direction,
        "score": score,
        "strength": strength,
        "confidence": confidence,
        "confidence_note": "How strongly the evidence agrees (0–95). Not a probability of profit.",
        "lean": lean,
        "agreement_pct": round(agreement * 100),
        "avg_adx_h1_h4": round(avg_adx, 1),
        "reasons": (reasons + reasons_extra) if direction != "NEUTRAL" else [],
        "evidence_bullish": [f for f in (_tf_fact(avail[k], 1) for k in ("D1", "H4", "H1") if k in avail) if f],
        "evidence_bearish": [f for f in (_tf_fact(avail[k], -1) for k in ("D1", "H4", "H1") if k in avail) if f],
        "against": against,
        "conflict": conflict,
    }


# --------------------------------------------------------------------------- invalidation + scenarios
def invalidation_and_scenarios(verdict: dict, h1: dict, h4: dict | None) -> dict:
    st = h1["structure"]
    res = h1["resistances"][0]["level"] if h1["resistances"] else st.get("last_swing_high")
    sup = h1["supports"][0]["level"] if h1["supports"] else st.get("last_swing_low")
    inval = []
    if verdict["direction"] == "BEARISH":
        if st.get("last_swing_high"):
            inval.append(f"An H1 candle closing above {st['last_swing_high']:.2f} (the last H1 swing high) breaks the bearish structure.")
        if h4 and h4.get("ema50"):
            inval.append(f"An H4 close back above the H4 EMA 50 ({h4['ema50']:.2f}) would cancel the bearish trend reading.")
    elif verdict["direction"] == "BULLISH":
        if st.get("last_swing_low"):
            inval.append(f"An H1 candle closing below {st['last_swing_low']:.2f} (the last H1 swing low) breaks the bullish structure.")
        if h4 and h4.get("ema50"):
            inval.append(f"An H4 close back below the H4 EMA 50 ({h4['ema50']:.2f}) would cancel the bullish trend reading.")
    else:
        if res:
            inval.append(f"A bullish case needs an H1 close above resistance {res:.2f} with H1 and H4 turning bullish.")
        if sup:
            inval.append(f"A bearish case needs an H1 close below support {sup:.2f} with H1 and H4 turning bearish.")
    bull = f"Bullish scenario: holds above {sup:.2f}, then an H1 close above {res:.2f} opens the next resistance." if sup and res else None
    bear = f"Bearish scenario: rejected below {res:.2f}, then an H1 close below {sup:.2f} opens the next support." if sup and res else None
    return {"invalidation": inval, "bullish_scenario": bull, "bearish_scenario": bear}


# --------------------------------------------------------------------------- trade plan
def trade_plan(verdict: dict, h1: dict, price: float, data_status: str, basis: float | None,
               chart_delay_min: float | None, params: dict | None = None) -> dict:
    """Educational setup built only from real levels. Returns action BUY / SELL / NO TRADE."""
    P = {**DEFAULT_PARAMS, **(params or {})}
    avoid = [
        "High-impact US news (CPI, NFP, FOMC, PCE) within the next 30 minutes – the economic calendar arrives in Phase 5; check ForexFactory until then.",
        "Your broker's spread is wider than usual.",
        "Price is already beyond the entry zone – do not chase.",
    ]
    if chart_delay_min and chart_delay_min > 5:
        avoid.insert(0, f"The chart data is about {round(chart_delay_min)} min delayed – confirm the zone on your broker's live chart before acting.")

    def no_trade(reason: str, wait_for: list[str]) -> dict:
        return {"action": "NO TRADE", "headline": "NO TRADE – WAIT FOR CONFIRMATION", "reason": reason,
                "wait_for": wait_for, "setup": None, "avoid_when": avoid}

    st = h1["structure"]
    res = h1["resistances"][0] if h1["resistances"] else None
    sup = h1["supports"][0] if h1["supports"] else None
    wait_generic = []
    if res:
        wait_generic.append(f"BUY idea only after an H1 close above {res['level']:.2f} with H1 and H4 bullish.")
    if sup:
        wait_generic.append(f"SELL idea only after an H1 close below {sup['level']:.2f} with H1 and H4 bearish.")

    if data_status not in ("LIVE",):
        return no_trade(f"Market data is {data_status.replace('_', ' ').lower()} – no setup is shown on old data.", wait_generic)
    if verdict["direction"] == "NEUTRAL":
        why = verdict["conflict"] or "Timeframes and indicators do not agree on a direction."
        return no_trade(why, wait_generic)
    if (verdict["confidence"] or 0) < P["min_confidence"]:
        return no_trade(f"Evidence agreement is {verdict['confidence']}/100 – below the {P['min_confidence']} needed for a setup.", wait_generic)
    if P["rsi_filter"] and h1.get("rsi") is not None:
        lo_rsi, hi_rsi = P["rsi_filter"], 100 - P["rsi_filter"]
        if verdict["direction"] == "BEARISH" and h1["rsi"] < lo_rsi:
            return no_trade(f"H1 RSI {h1['rsi']} is stretched (below {lo_rsi}) – wait for a bounce before selling.", wait_generic)
        if verdict["direction"] == "BULLISH" and h1["rsi"] > hi_rsi:
            return no_trade(f"H1 RSI {h1['rsi']} is stretched (above {hi_rsi}) – wait for a dip before buying.", wait_generic)
    if h1["adx"] is not None and h1["adx"] < P["min_adx"]:
        return no_trade(f"H1 ADX is {h1['adx']} – the trend is too weak for a trend-following entry.", wait_generic)

    atr, ema20 = h1["atr"], h1["ema20"]
    if not atr or ema20 is None:
        return no_trade("Not enough H1 data for ATR/EMA-based levels.", wait_generic)

    # Pullback zone = between the H1 EMA 20 and the nearest level on the trade's "wrong" side
    # (resistance for a sell, support for a buy) if that level is within 3×ATR.
    if verdict["direction"] == "BEARISH":
        side, action = -1, "SELL"
        near = res is not None and res["level"] - price <= 3 * atr
        ref = res["level"] if near else ema20 + 0.5 * atr
        z_lo, z_hi = max(price, min(ema20, ref)), max(price, max(ema20, ref))
        target_lv = sup["level"] if sup else None
    else:
        side, action = 1, "BUY"
        near = sup is not None and price - sup["level"] <= 3 * atr
        ref = sup["level"] if near else ema20 - 0.5 * atr
        z_lo, z_hi = min(price, min(ema20, ref)), min(price, max(ema20, ref))
        target_lv = res["level"] if res else None

    if z_hi - z_lo < 0.2 * atr:                      # give the zone a minimum width
        mid = (z_hi + z_lo) / 2
        z_lo, z_hi = mid - 0.15 * atr, mid + 0.15 * atr
    entry = (z_lo + z_hi) / 2
    # Stop: beyond the WHOLE zone and beyond the level's own zone, plus 0.5×ATR buffer,
    # and never closer than 1×ATR to the entry (inside normal H1 noise).
    # The last H1 swing point beyond the zone is the structure the trade depends on:
    # if it lies within 1.5×ATR of the zone, the stop must go beyond it as well.
    if side < 0:
        anchor = max(z_hi, res["zone_high"] if near else z_hi)
        swing = st.get("last_swing_high")
        if swing and z_hi <= swing <= z_hi + 1.5 * atr:
            anchor = max(anchor, swing)
        stop = max(anchor + P["stop_buffer_atr"] * atr, entry + P["min_stop_atr"] * atr)
    else:
        anchor = min(z_lo, sup["zone_low"] if near else z_lo)
        swing = st.get("last_swing_low")
        if swing and z_lo - 1.5 * atr <= swing <= z_lo:
            anchor = min(anchor, swing)
        stop = min(anchor - P["stop_buffer_atr"] * atr, entry - P["min_stop_atr"] * atr)
    risk = abs(stop - entry)
    if risk <= 0 or risk > P["max_risk_atr"] * atr:
        return no_trade(f"The nearest valid stop would be {risk / atr:.1f}× ATR away – too wide for a sound setup.", wait_generic)
    tp2 = entry + side * P["target_r"] * risk
    tp1 = target_lv if target_lv is not None and (target_lv - entry) * side > 0.5 * atr else None
    rr1 = abs(tp1 - entry) / risk if tp1 is not None else None
    if rr1 is not None and rr1 < MIN_RR_FIRST_TARGET:
        lvl = "support" if side < 0 else "resistance"
        return no_trade(
            f"Poor reward-to-risk: the stop must be ${risk:.2f} away ({risk / atr:.1f}× ATR) but the first {lvl} "
            f"at {tp1:.2f} is only {rr1:.2f}R from the entry. Price often bounces there.",
            wait_generic,
        )

    distance_atr = (z_lo - price) / atr if side < 0 else (price - z_hi) / atr
    if z_lo <= price <= z_hi:
        timing = "Price is inside the zone now – wait for an M15 candle to close in the trade direction (rejection) before entering."
    elif distance_atr > 1.5:
        timing = f"Zone is {distance_atr:.1f}× ATR away – wait for price to pull back into it; do not enter at market."
    else:
        timing = "Wait for price to reach the zone, then for an M15 candle to close in the trade direction."

    def spot(x):
        return None if x is None or basis is None else round(x - basis, 2)

    if P["entry_mode"] == "confirm":
        confirm_line = (f"Then an H1 candle CLOSES {'below its open (red)' if side < 0 else 'above its open (green)'} with the close "
                        f"{'at or below ' + format(z_hi, '.2f') if side < 0 else 'at or above ' + format(z_lo, '.2f')} – enter at that close "
                        f"(strategy A). Valid for 12 H1 candles.")
        if z_lo <= price <= z_hi:
            timing = "Price is in the zone – wait for an H1 candle to close in the trade direction before entering (strategy A)."
        elif distance_atr > 1.5:
            timing = f"Zone is {distance_atr:.1f}× ATR away – wait for price to come back to it, then for an H1 confirmation close."
        else:
            timing = "Wait for price to reach the zone, then for an H1 candle to close in the trade direction (strategy A)."
    else:
        confirm_line = f"An M15 candle closes {'below' if side < 0 else 'above'} its open inside the zone (rejection in the trade direction)."
    enter_when = [
        f"Price trades into the {action} zone {z_lo:.2f}–{z_hi:.2f} (chart series).",
        confirm_line,
        f"H1 has not closed {'above' if side < 0 else 'below'} {stop + side * P["stop_buffer_atr"] * atr:.2f} (structure still intact).",
        "Risk per trade stays within your 1–2% rule at the stop distance shown.",
    ]
    if not tp1 or (rr1 is not None and rr1 < 1.5):
        enter_when.append("Nearest opposite level gives less than 1.5R – consider TP2 (2R) only if momentum confirms.")

    return {
        "action": action,
        "headline": f"{action} SETUP – {'pullback sell' if side < 0 else 'pullback buy'} on H1"
                    + (" with H1 confirmation (strategy A)" if P["entry_mode"] == "confirm" else "") + " – educational",
        "reason": f"{verdict['direction'].title()} verdict, confidence {verdict['confidence']}/100, trend strength {verdict['strength'].lower()}.",
        "timing": timing,
        "setup": {
            "timeframe": "H1 levels, M15 confirmation",
            "entry_zone": [round(z_lo, 2), round(z_hi, 2)],
            "entry_mid": round(entry, 2),
            "stop_loss": round(stop, 2),
            "take_profit_1": round(tp1, 2) if tp1 is not None else None,
            "take_profit_2": round(tp2, 2),
            "risk_per_oz": round(risk, 2),
            "risk_atr": round(risk / atr, 2),
            "rr_tp1": round(rr1, 2) if rr1 is not None else None,
            "rr_tp2": P["target_r"],
            "spot_equivalent": {
                "basis_used": round(basis, 2) if basis is not None else None,
                "entry_zone": [spot(z_lo), spot(z_hi)],
                "stop_loss": spot(stop),
                "take_profit_1": spot(tp1),
                "take_profit_2": spot(tp2),
                "note": "Chart levels minus the current chart-vs-spot gap. The gap moves; re-check on your broker's chart.",
            },
            "built_from": {
                "h1_ema20": ema20,
                "h1_atr14": atr,
                "nearest_resistance": res["level"] if res else None,
                "nearest_support": sup["level"] if sup else None,
                "last_swing_high": st.get("last_swing_high"),
                "last_swing_low": st.get("last_swing_low"),
            },
        },
        "enter_when": enter_when,
        "avoid_when": avoid,
        "wait_for": [],
    }
