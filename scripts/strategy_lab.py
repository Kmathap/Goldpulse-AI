"""Compare 8 versions of the GoldPulse trade rules fairly (walk-forward).

Run from the project folder:
    python scripts\\strategy_lab.py

Takes about 1–2 minutes. The winner is picked on the FIRST half of the data;
its SECOND-half result (never used for picking) is the honest test.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import PROJECT_ROOT, load_settings  # noqa: E402
from backend.data.base import DataUnavailableError  # noqa: E402
from backend.strategy_lab import walk_forward  # noqa: E402


def build_provider(settings):
    if settings.resolved_provider == "twelvedata":
        from backend.data.twelvedata_provider import TwelveDataProvider
        return TwelveDataProvider(settings)
    from backend.data.xaus_provider import XausProvider
    return XausProvider(settings)


def fmt(s: dict) -> str:
    if not s["trades"]:
        return f"{'0':>4} {'–':>6} {'–':>7} {'–':>5}"
    pf = "∞" if s["pf"] >= 99 else f"{s['pf']:.2f}"
    return f"{s['trades']:>4} {s['win_rate']:>5.0f}% {s['avg_r']:>+7.2f} {pf:>5}"


def main() -> int:
    print("GoldPulse AI – strategy lab (walk-forward test of 8 rule versions)\n")
    provider = build_provider(load_settings())
    try:
        h1 = provider.get_history("H1").df.iloc[:-1]
        d1 = provider.get_history("D1").df
    except DataUnavailableError as exc:
        print(f"RESULT: DATA UNAVAILABLE – {exc}")
        return 1
    print(f"  H1 candles: {len(h1)}   D1 candles: {len(d1)}")
    print("  Evaluating the trend engine at every H1 close (about a minute)…")
    t0 = time.time()
    res = walk_forward(h1, d1, progress=lambda i, n: print(f"    {i}/{n}"))
    print(f"  Done in {time.time() - t0:.0f} s\n")

    print(f"  Choosing period (first half) : {res['period_start'][:16]} → {res['split'][:16]} UTC")
    print(f"  Unseen test (second half)    : {res['split'][:16]} → {res['period_end'][:16]} UTC\n")
    head = f"{'trades':>4} {'win':>6} {'avg R':>7} {'PF':>5}"
    print(f"  {'Version':<28} | FIRST HALF (choose)        | SECOND HALF (unseen test)  | WHOLE PERIOD")
    print(f"  {'':<28} | {head}  | {head}  | {head}")
    print("  " + "-" * 110)
    for r in res["rows"]:
        mark = "  ◄ chosen" if r["key"] == res["chosen"] else ""
        print(f"  {r['name']:<28} | {fmt(r['first'])}  | {fmt(r['second'])}  | {fmt(r['all'])}{mark}")
    print("\n  avg R = average result per trade in units of risk (+0.15 or better is the bar). PF = profit factor.")
    print("\nVERDICT")
    print(f"  {res['verdict']['state']}: {res['verdict']['text']}")

    out = PROJECT_ROOT / "backtest_results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    with open(out / f"lab_trades_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
        if res["trades"]:
            w = csv.DictWriter(f, fieldnames=list(res["trades"][0].keys()))
            w.writeheader()
            w.writerows(res["trades"])
    with open(out / f"lab_summary_{stamp}.json", "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in res.items() if k != "trades"}, f, indent=2, default=str)
    print(f"\n  Saved: backtest_results\\lab_trades_{stamp}.csv and lab_summary_{stamp}.json")
    print("\nNote: about 6 weeks per half is a small sample. Past results do not guarantee future results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
