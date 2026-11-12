import numpy as np
import pandas as pd

from backend.backtest import run_backtest
from backend.strategy_lab import VARIANTS, build_snapshots, judge, simulate, stats, walk_forward
from test_backtest import synthetic


def test_current_variant_matches_backtest_exactly():
    h1, d1 = synthetic()
    bt = [t for t in run_backtest(h1, d1)["trade_list"] if t["result"] != "NOT_FILLED"]
    lab = simulate(h1, build_snapshots(h1, d1), VARIANTS[0])
    assert [round(t["r_multiple"], 3) for t in bt] == [t["r"] for t in lab]
    assert [t["entry"] for t in bt] == [t["entry"] for t in lab]


def test_every_variant_produces_consistent_trades():
    h1, d1 = synthetic()
    snaps = build_snapshots(h1, d1)
    for v in VARIANTS:
        for t in simulate(h1, snaps, v):
            if t["side"] == "SELL":
                assert t["stop"] > t["entry"] > t["target"]
            else:
                assert t["stop"] < t["entry"] < t["target"]
            assert t["exit_time"] >= t["signal_time"]


def test_choice_uses_first_half_only():
    h1, d1 = synthetic()
    a = walk_forward(h1, d1)
    split = pd.Timestamp(a["split"])
    h1b = h1.copy()
    late = h1b.index >= split + pd.Timedelta(hours=60)          # scramble data well after the split
    h1b.loc[late, ["open", "high", "low", "close"]] = h1b.loc[late, ["open", "high", "low", "close"]].to_numpy()[::-1]
    h1b.loc[late, "high"] = h1b.loc[late, ["open", "close"]].max(axis=1) + 3
    h1b.loc[late, "low"] = h1b.loc[late, ["open", "close"]].min(axis=1) - 3
    b = walk_forward(h1b, d1)
    assert a["chosen"] == b["chosen"]
    fa = {r["key"]: r["first"] for r in a["rows"]}
    fb = {r["key"]: r["first"] for r in b["rows"]}
    assert fa == fb


def test_judge_states():
    mk = lambda n, avg, pf: {"name": "X", "second": {"trades": n, "avg_r": avg, "pf": pf}}  # noqa: E731
    assert judge(None)["state"] == "NO_CANDIDATE"
    assert judge(mk(4, 1.0, 3))["state"] == "TOO_FEW"
    assert judge(mk(20, 0.3, 1.6))["state"] == "PROMISING"
    assert judge(mk(20, 0.05, 1.1))["state"] == "WEAK"
    assert judge(mk(20, -0.2, 0.7))["state"] == "FAILED"
    assert stats([])["trades"] == 0
