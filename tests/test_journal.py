import json

import pandas as pd

from backend.analysis import trend_engine as te
from backend.data.base import CandleSet
from backend.journal import Journal
from backend.strategy_lab import build_snapshots, get_variant, live_params, simulate
from test_backtest import synthetic


class Provider:
    def __init__(self, h1, d1):
        self.h1, self.d1 = h1, d1

    def get_candles(self, tf):
        df = self.h1 if tf == "H1" else self.d1
        return CandleSet(tf, df, "synthetic", (), df.index[-1].to_pydatetime() + pd.Timedelta(minutes=59))


def test_journal_matches_lab_and_persists(tmp_path):
    h1, d1 = synthetic()
    end = h1.index[-1].timestamp() + 3600 + 59 * 60
    start = h1.index[400].to_pydatetime()
    j = Journal(Provider(h1, d1), tmp_path, clock=lambda: end, start_at=start)
    st = j.update()
    assert st["error"] is None and st["current"]["state"] in {"WAITING", "PENDING", "OPEN"}

    # same trades as the lab's simulate() restricted to signals after the start
    snaps = build_snapshots(h1, d1)
    snaps = [s if (s is not None and h1.index[i] + pd.Timedelta(hours=1) > pd.Timestamp(start)) else None for i, s in enumerate(snaps)]
    lab = simulate(h1, snaps, get_variant("A"))
    assert [t["r"] for t in sorted(st["trades"], key=lambda t: t["signal_time"])] == [t["r"] for t in lab]
    assert st["stats"]["trades"] == len(lab)

    # a fresh Journal reloads everything from disk (no recomputation needed)
    j2 = Journal(Provider(h1, d1), tmp_path, clock=lambda: end)
    assert j2.status()["stats"] == st["stats"]
    assert json.loads((tmp_path / "meta.json").read_text())["variant"] == "A"


def test_journal_only_records_after_start(tmp_path):
    h1, d1 = synthetic()
    end = h1.index[-1].timestamp() + 3600 + 59 * 60
    j = Journal(Provider(h1, d1), tmp_path, clock=lambda: end)      # starts "now": no history back-filled
    st = j.update()
    assert st["stats"]["trades"] == 0 and st["current"]["state"] == "WAITING"


def test_live_plan_uses_strategy_a_wording():
    from test_trend_engine import all_tfs
    tfs = all_tfs(-1)
    plan = te.trade_plan(te.combine(tfs), tfs["H1"], tfs["H1"]["close"], "LIVE", 0, 0, params=live_params())
    assert plan["action"] == "SELL" and "strategy A" in plan["headline"]
    assert any("H1 candle CLOSES below its open" in x for x in plan["enter_when"])
