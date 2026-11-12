import numpy as np
import pandas as pd

from backend.backtest import WARMUP_H1, run_backtest, verdict_on_results


def synthetic(n=900, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-08-01", periods=n, freq="1h", tz="UTC")
    x = np.arange(n)
    trend = np.where(x < n // 2, 0.35 * x, 0.35 * (n // 2) - 0.35 * (x - n // 2))   # up, then down
    close = 4000 + trend + 10 * np.sin(x / 7) + rng.normal(0, 1.5, n).cumsum() * 0.2
    h1 = pd.DataFrame({"open": close - 0.5, "high": close + 3, "low": close - 3, "close": close, "volume": 1.0}, index=idx)
    d1 = h1.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    # need >= 60 D1 candles of history before the H1 window starts
    pre = pd.date_range(end=d1.index[0] - pd.Timedelta(days=1), periods=120, freq="1D", tz="UTC")
    c0 = 3900 + np.arange(120) * 0.8
    d1 = pd.concat([pd.DataFrame({"open": c0, "high": c0 + 15, "low": c0 - 15, "close": c0, "volume": 1.0}, index=pre), d1])
    return h1, d1


def test_backtest_runs_and_reports():
    h1, d1 = synthetic()
    s = run_backtest(h1, d1)
    assert s["h1_candles_tested"] == len(h1) - WARMUP_H1
    assert s["signals"] >= s["trades"]
    for t in s["trade_list"]:
        if t["result"] in ("WIN", "LOSS", "TIMEOUT"):
            assert t["fill_time"] >= t["signal_time"] and t["exit_time"] >= t["fill_time"]
            if t["result"] == "LOSS":
                assert t["r_multiple"] < -1                   # full stop plus costs
            if t["result"] == "WIN":
                assert 1.9 < t["r_multiple"] <= 2.0           # 2R minus costs
    assert isinstance(verdict_on_results(s), list)


def test_no_lookahead_prefix_gives_same_early_trades():
    # Running on data that stops at candle 700 must reproduce exactly the same trades the
    # full run made up to that point – proof that no future candle influenced a decision.
    h1, d1 = synthetic()
    full = run_backtest(h1, d1)["trade_list"]
    part = run_backtest(h1[h1.index <= h1.index[700]], d1)["trade_list"]
    assert len(part) >= 5
    assert part[:-1] == full[: len(part) - 1]      # the last one may still have been open at the cut


def test_latest_status_states(tmp_path):
    import json
    from backend.backtest import latest_status
    assert latest_status(tmp_path)["state"] == "NOT_TESTED"
    (tmp_path / "summary_20261111_2115.json").write_text(json.dumps(
        {"trades": 25, "avg_r": -0.182, "win_rate_pct": 28.0, "profit_factor": 0.75,
         "period_start": "2026-08-26T10:00", "period_end": "2026-11-11T14:00"}))
    st = latest_status(tmp_path)
    assert st["state"] == "FAILED" and "-0.18 R" in st["message"] and "do not trade" in st["message"]
    (tmp_path / "summary_20261201_1000.json").write_text(json.dumps(
        {"trades": 40, "avg_r": 0.3, "win_rate_pct": 45.0, "profit_factor": 1.6}))
    assert latest_status(tmp_path)["state"] == "PASSED"
