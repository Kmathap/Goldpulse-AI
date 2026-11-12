"""Backtest the GoldPulse BUY/SELL rules on real past gold data.

Run from the project folder:
    python scripts\\run_backtest.py

Takes about 1 minute. Uses the same data feed as the dashboard
(free xaus.com: ~3 months of H1 candles, 2 years of D1).
Results are printed and saved in the backtest_results folder.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.backtest import COST_PER_OZ, run_backtest, verdict_on_results  # noqa: E402
from backend.config import PROJECT_ROOT, load_settings  # noqa: E402
from backend.data.base import DataUnavailableError  # noqa: E402


def build_provider(settings):
    if settings.resolved_provider == "twelvedata":
        from backend.data.twelvedata_provider import TwelveDataProvider
        return TwelveDataProvider(settings)
    from backend.data.xaus_provider import XausProvider
    return XausProvider(settings)


def main() -> int:
    print("GoldPulse AI – backtest of the BUY/SELL rules\n")
    provider = build_provider(load_settings())
    try:
        h1 = provider.get_history("H1")
        d1 = provider.get_history("D1")
    except DataUnavailableError as exc:
        print(f"RESULT: DATA UNAVAILABLE – {exc}")
        return 1
    h1_df = h1.df.iloc[:-1]          # drop the still-forming candle
    print(f"  Data      : {h1.source}")
    print(f"  H1 candles: {len(h1_df)}  ({h1_df.index[0]:%d %b %Y} → {h1_df.index[-1]:%d %b %Y %H:%M} UTC)")
    print(f"  D1 candles: {len(d1.df)}")
    print(f"  Costs     : ${COST_PER_OZ:.2f} per oz per trade (spread + commission + slippage)\n")
    print("  Running (about a minute)…")
    t0 = time.time()
    s = run_backtest(h1_df, d1.df)
    print(f"  Done in {time.time() - t0:.0f} s\n")

    print("RESULTS")
    print(f"  Period tested        : {s['period_start'][:16]} → {s['period_end'][:16]} UTC")
    print(f"  Signals              : {s['signals']}  (not filled: {s['not_filled']})")
    print(f"  Completed trades     : {s['trades']}   wins {s['wins']} / losses {s['losses']} / timeouts {s['timeouts']}")
    print(f"  Win rate             : {s['win_rate_pct']}%")
    print(f"  Average per trade    : {s['avg_r']} R")
    print(f"  Total                : {s['total_r']} R")
    print(f"  Profit factor        : {s['profit_factor']}")
    print(f"  Worst losing streak  : {s['max_losing_streak']} trades")
    print(f"  Max drawdown         : {s['max_drawdown_r']} R")
    print(f"  At 1% risk per trade : return {s['return_pct_at_1pct_risk']}%, max drawdown {s['max_drawdown_pct_at_1pct_risk']}%")
    print(f"  Reached TP1 before SL: {s['tp1_reached_before_stop_pct']}% of trades that had a TP1")
    print("\n  Most common reasons for NO TRADE:")
    for reason, count in list(s["blocked_signals"].items())[:5]:
        print(f"    {count:5d} × {reason}")
    print("\nREADING")
    for note in verdict_on_results(s):
        print(f"  • {note}")

    out = PROJECT_ROOT / "backtest_results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    with open(out / f"trades_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        rows = s["trade_list"]
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    with open(out / f"summary_{stamp}.json", "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in s.items() if k != "trade_list"}, f, indent=2, default=str)
    print(f"\n  Saved: backtest_results\\trades_{stamp}.csv and summary_{stamp}.json")
    print("\nNote: past results do not guarantee future results. News filter and M15 confirmation are not modelled;")
    print("the free data is a delayed, possibly futures-based series.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
