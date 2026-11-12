import numpy as np
import pandas as pd
import pytest

from backend.analysis import indicators as ind
from backend.analysis import trend_engine as te
from backend.analysis.structure import breakout_check, find_swings, levels, structure


def frame(direction=1, n=400, step="1h", start=4000.0, slope=0.4, wave=12.0):
    idx = pd.date_range("2026-10-01", periods=n, freq=step, tz="UTC")
    x = np.arange(n)
    close = start + direction * slope * x - direction * wave * np.sin(x / 6.0)   # mirror images
    df = pd.DataFrame({"open": close - 0.3, "high": close + 2.0, "low": close - 2.0, "close": close, "volume": 1.0}, index=idx)
    return ind.compute_all(df)


def all_tfs(direction):
    return {tf: te.evaluate_timeframe(tf, frame(direction)) for tf in te.TF_WEIGHTS}


# --------------------------------------------------------------- structure helpers
def test_swings_and_structure_uptrend():
    d = frame(1)
    st = structure(find_swings(d))
    assert st["label"] == "UPTREND" and st["last_swing_low"] > st["prev_swing_low"]


def test_structure_downtrend():
    assert structure(find_swings(frame(-1)))["label"] == "DOWNTREND"


def test_levels_split_around_price():
    d = frame(1)
    sw = find_swings(d)
    price = float(d["close"].iloc[-1])
    lv = levels(sw, price, atr=5.0)
    assert all(x["level"] < price for x in lv["supports"])
    assert all(x["level"] > price for x in lv["resistances"])
    assert lv["supports"] == sorted(lv["supports"], key=lambda x: -x["level"])


def test_breakout_and_false_breakout():
    idx = pd.date_range("2026-11-01", periods=10, freq="1h", tz="UTC")
    close = np.array([99, 99, 99, 99, 99, 99, 99, 99, 99, 102.0])
    df = pd.DataFrame({"high": close + 0.5, "low": close - 0.5, "close": close}, index=idx)
    assert breakout_check(df, [{"level": 100.0}], atr=2.0)["type"] == "BREAKOUT_UP"
    close2 = close.copy(); close2[-1] = 99.5
    high2 = close2 + 0.5; high2[-2] = 101.5
    df2 = pd.DataFrame({"high": high2, "low": close2 - 0.5, "close": close2}, index=idx)
    assert breakout_check(df2, [{"level": 100.0}], atr=2.0)["type"] == "FALSE_BREAKOUT_UP"


# --------------------------------------------------------------- verdicts
def test_bullish_verdict_with_reasons():
    v = te.combine(all_tfs(1))
    assert v["direction"] == "BULLISH" and 60 <= v["confidence"] <= 95
    assert v["reasons"] and v["confidence_note"].startswith("How strongly")


def test_bearish_verdict():
    v = te.combine(all_tfs(-1))
    assert v["direction"] == "BEARISH" and v["confidence"] >= 60


def test_conflict_forces_neutral():
    tfs = all_tfs(1)
    tfs["H1"] = te.evaluate_timeframe("H1", frame(-1))      # H1 strongly bearish
    tfs["H4"] = te.evaluate_timeframe("H4", frame(-1))
    v = te.combine(tfs)
    assert v["direction"] == "NEUTRAL" and v["confidence"] is None


def test_confidence_never_100():
    assert te.combine(all_tfs(1))["confidence"] <= 95


# --------------------------------------------------------------- trade plan
@pytest.mark.parametrize("direction,action", [(1, "BUY"), (-1, "SELL")])
def test_plan_levels_are_consistent(direction, action):
    tfs = all_tfs(direction)
    v = te.combine(tfs)
    h1 = tfs["H1"]
    plan = te.trade_plan(v, h1, h1["close"], "LIVE", basis=10.0, chart_delay_min=11)
    assert plan["action"] == action
    s = plan["setup"]
    lo, hi = s["entry_zone"]
    assert lo <= hi
    if action == "BUY":
        assert s["stop_loss"] < lo and s["take_profit_2"] > hi
    else:
        assert s["stop_loss"] > hi and s["take_profit_2"] < lo
    assert s["risk_per_oz"] >= h1["atr"] * 0.99                        # never inside 1×ATR noise
    assert s["take_profit_2"] == pytest.approx(s["entry_mid"] + direction * 2 * s["risk_per_oz"], abs=0.02)
    assert s["spot_equivalent"]["stop_loss"] == pytest.approx(s["stop_loss"] - 10.0)
    assert any("delayed" in a for a in plan["avoid_when"])


def test_no_trade_when_neutral_or_not_live():
    tfs = all_tfs(1)
    v = te.combine(tfs)
    assert te.trade_plan(v, tfs["H1"], tfs["H1"]["close"], "STALE", 0, 0)["action"] == "NO TRADE"
    neutral = {**v, "direction": "NEUTRAL", "confidence": None}
    p = te.trade_plan(neutral, tfs["H1"], tfs["H1"]["close"], "LIVE", 0, 0)
    assert p["action"] == "NO TRADE" and p["headline"] == "NO TRADE – WAIT FOR CONFIRMATION" and p["setup"] is None


def test_low_confidence_no_trade():
    tfs = all_tfs(1)
    v = {**te.combine(tfs), "confidence": 45}
    assert te.trade_plan(v, tfs["H1"], tfs["H1"]["close"], "LIVE", 0, 0)["action"] == "NO TRADE"


def test_pullback_inside_uptrend_is_not_called_bullish():
    # Uptrend whose newest candles are falling (a pullback): engine must not force a BUY.
    idx_n = 400
    x = np.arange(idx_n)
    close = 4000 + 0.4 * x + 12 * np.sin(x / 6.0)       # ends while the wave is falling
    idx = pd.date_range("2026-10-01", periods=idx_n, freq="1h", tz="UTC")
    d = ind.compute_all(pd.DataFrame({"open": close, "high": close + 2, "low": close - 2, "close": close, "volume": 1.0}, index=idx))
    v = te.combine({tf: te.evaluate_timeframe(tf, d) for tf in te.TF_WEIGHTS})
    assert v["direction"] == "NEUTRAL"


def test_sell_stop_sits_beyond_last_swing_high():
    # Live case 11 Nov 2026: zone 4058.88–4061.77, ATR 13.2, last H1 swing high 4074.70.
    # The old rule put the stop at 4073.52 – inside the swing high it is meant to protect.
    h1 = {
        "atr": 13.2, "ema20": 4058.88, "adx": 19.9,
        "structure": {"last_swing_high": 4074.70, "last_swing_low": 4034.10, "score": 1, "text": ""},
        "resistances": [{"level": 4061.77, "zone_low": 4059.5, "zone_high": 4063.4, "touches": 3}],
        "supports": [{"level": 4037.93, "zone_low": 4036.0, "zone_high": 4039.0, "touches": 4}],
    }
    verdict = {"direction": "BEARISH", "confidence": 69, "strength": "MODERATE", "conflict": None}
    plan = te.trade_plan(verdict, h1, price=4022.4, data_status="LIVE", basis=5.0, chart_delay_min=12)
    s = plan["setup"]
    assert s["stop_loss"] > 4074.70
    assert s["stop_loss"] == pytest.approx(4074.70 + 0.5 * 13.2, abs=0.01)
    assert s["take_profit_2"] == pytest.approx(s["entry_mid"] - 2 * s["risk_per_oz"], abs=0.02)


def test_buy_stop_sits_beyond_last_swing_low():
    h1 = {
        "atr": 10.0, "ema20": 4050.0, "adx": 25.0,
        "structure": {"last_swing_high": 4090.0, "last_swing_low": 4035.0, "score": 1, "text": ""},
        "resistances": [{"level": 4090.0, "zone_low": 4088.0, "zone_high": 4092.0, "touches": 2}],
        "supports": [{"level": 4045.0, "zone_low": 4043.0, "zone_high": 4047.0, "touches": 3}],
    }
    verdict = {"direction": "BULLISH", "confidence": 75, "strength": "STRONG", "conflict": None}
    s = te.trade_plan(verdict, h1, price=4070.0, data_status="LIVE", basis=0.0, chart_delay_min=0)["setup"]
    assert s["stop_loss"] < 4035.0


def test_false_breakdown_listed_against_bearish_verdict():
    tfs = all_tfs(-1)
    tfs["H4"] = {**tfs["H4"], "breakout": {"type": "FALSE_BREAKDOWN", "level": 4037.52,
                                          "text": "Dropped below 4037.52 but closed back above it (failed breakdown)"}}
    v = te.combine(tfs)
    assert any("failed breakdown" in a for a in v["against"])


def test_live_case_2049_ist_stop_and_structure_line():
    # Live inputs at 20:49 IST, 11 Nov 2026 (read from the dashboard)
    h1 = {
        "atr": 14.24, "ema20": 4055.40, "adx": 21.5,
        "structure": {"last_swing_high": 4074.70, "last_swing_low": 4034.10, "score": 1, "text": ""},
        "resistances": [{"level": 4037.93, "zone_low": 4034.1, "zone_high": 4044.5, "touches": 4}],
        "supports": [],      # no nearby support, so the R:R gate does not apply here
    }
    verdict = {"direction": "BEARISH", "confidence": 83, "strength": "MODERATE", "conflict": None}
    plan = te.trade_plan(verdict, h1, price=4024.30, data_status="LIVE", basis=8.4, chart_delay_min=12)
    s = plan["setup"]
    assert s["stop_loss"] == pytest.approx(4074.70 + 0.5 * 14.24, abs=0.01)       # 4081.82
    assert any("not closed above 4074.70" in x for x in plan["enter_when"])       # the swing high itself


def test_live_case_2053_poor_reward_to_risk_means_no_trade():
    # 20:53 IST: stop $35.15 away, first support 4020.80 only 0.74R -> must not be a SELL
    h1 = {
        "atr": 14.24, "ema20": 4055.40, "adx": 21.5,
        "structure": {"last_swing_high": 4074.70, "last_swing_low": 4034.10, "score": -1, "text": ""},
        "resistances": [{"level": 4037.93, "zone_low": 4034.1, "zone_high": 4044.5, "touches": 4}],
        "supports": [{"level": 4020.80, "zone_low": 4020.8, "zone_high": 4020.8, "touches": 1}],
    }
    verdict = {"direction": "BEARISH", "confidence": 83, "strength": "MODERATE", "conflict": None}
    plan = te.trade_plan(verdict, h1, price=4022.40, data_status="LIVE", basis=10.7, chart_delay_min=11)
    assert plan["action"] == "NO TRADE"
    assert "0.74R" in plan["reason"] and "4020.80" in plan["reason"]
    assert any("close below 4020.80" in w for w in plan["wait_for"])


def test_uptrend_broken_when_close_below_last_higher_low():
    from backend.analysis.structure import Swing
    from datetime import datetime, timezone
    t = datetime(2026, 11, 11, tzinfo=timezone.utc)
    sw = [Swing("high", 4060.6, t, 1), Swing("low", 4020.8, t, 2), Swing("high", 4074.7, t, 3), Swing("low", 4034.1, t, 4)]
    assert structure(sw)["label"] == "UPTREND"
    st = structure(sw, close=4022.4)
    assert st["label"] == "UPTREND BROKEN" and st["score"] == -1 and "4034.10" in st["text"]
    assert structure(sw, close=4050.0)["label"] == "UPTREND"
