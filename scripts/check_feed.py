"""Step-1 check: can GoldPulse AI reach its gold price feed?

Run from the project folder:
    python scripts\\check_feed.py

Uses the free xaus.com feed, or Twelve Data if TWELVEDATA_API_KEY is set in .env.
No MT5 and no broker account are involved.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.api.main import build_provider  # noqa: E402
from backend.config import load_settings  # noqa: E402
from backend.data.base import TIMEFRAMES, DataUnavailableError  # noqa: E402
from backend.data.market_service import MarketService  # noqa: E402
from backend.timeutils import ist_label  # noqa: E402


def main() -> int:
    settings = load_settings()
    provider = build_provider(settings)
    info = provider.info()
    print("GoldPulse AI – data feed check\n")
    print(f"  Provider : {info.name}  ({info.cost})")
    ok = True

    try:
        q = provider.get_quote()
        print(f"  Spot     : {q.price:.2f} USD/oz  as of {ist_label(q.time_utc)}  [{q.source_state}]")
    except DataUnavailableError as exc:
        ok = False
        print(f"  Spot     : DATA UNAVAILABLE – {exc}")

    print("\n  Candles:")
    for tf in TIMEFRAMES:
        try:
            cs = provider.get_candles(tf)
            last = cs.df.iloc[-1]
            print(f"    {tf:>3}: {len(cs.df):5d} candles | last opened {ist_label(cs.df.index[-1].to_pydatetime())} "
                  f"| O {last.open:.2f} H {last.high:.2f} L {last.low:.2f} C {last.close:.2f}")
        except DataUnavailableError as exc:
            ok = False
            print(f"    {tf:>3}: DATA UNAVAILABLE – {exc}")

    if ok:
        snap = MarketService(provider, settings).price_snapshot()
        print(f"\n  Status   : {snap['status']}  {snap.get('status_reason') or ''}")
        print(f"  Chart feed: {snap['chart']['feed']}  (newest point {snap['chart']['last_point_ist']})")
        print(f"  Gap chart series vs spot: {snap['chart']['basis_vs_spot']} USD ({snap['chart']['basis_vs_spot_pct']}%)")
        for w in snap.get("warnings", []):
            print(f"  WARNING  : {w}")
        print("\nRESULT: OK – the gold data feed is reachable.")
        return 0
    print("\nRESULT: DATA UNAVAILABLE – copy this whole output to Claude.")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataUnavailableError as exc:
        print(f"\nRESULT: DATA UNAVAILABLE – {exc}")
        raise SystemExit(1)
    except Exception as exc:  # show everything needed for debugging, never secrets
        print(f"\nUnexpected error: {type(exc).__name__}: {exc}")
        print(json.dumps({"python": sys.version}, indent=2))
        raise SystemExit(2)
