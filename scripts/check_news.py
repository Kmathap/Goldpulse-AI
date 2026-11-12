"""Check the live news feeds:  python scripts\\check_news.py"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.analysis.news_scoring import build_news  # noqa: E402
from backend.data.base import DataUnavailableError  # noqa: E402
from backend.data.news_provider import NewsProvider  # noqa: E402


def main() -> int:
    print("GoldPulse AI – news feed check\n")
    try:
        items, errors = NewsProvider().fetch()
    except DataUnavailableError as exc:
        print(f"RESULT: DATA UNAVAILABLE – {exc}")
        return 1
    for feed, err in errors.items():
        print(f"  FEED PROBLEM  {feed}: {err}")
    n = build_news(items, datetime.now(timezone.utc))
    fs = n["fundamental_sentiment"]
    print(f"  Raw headlines: {len(items)}   Unique stories shown: {len(n['items'])}")
    print(f"  News sentiment (24 h): {fs['label']}  score {fs['score']}  "
          f"(bullish {fs['bullish']} / bearish {fs['bearish']} / neutral {fs['neutral']})\n")
    for x in n["items"][:12]:
        print(f"  [{x['published_ist'][-12:-4]}] {x['sentiment']:<19} {x['impact']:<6} {x['type']:<18} {x['source'][:18]:<18} {x['title'][:80]}")
    print("\nRESULT: OK – news feeds are reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
