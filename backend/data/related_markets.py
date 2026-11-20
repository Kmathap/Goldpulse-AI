"""Related markets: do the US dollar, silver and oil confirm what gold is doing?

One batched Twelve Data /quote call (one credit per symbol) every 60 seconds:

* XAU/USD  – gold itself, so all changes are measured the same way (vs previous daily close)
* EUR/USD, GBP/USD, USD/JPY – a simple US-dollar gauge (a stronger dollar usually weighs on gold)
* XAG/USD  – silver usually moves with gold; agreement makes a gold move more trustworthy
* WTI/USD  – oil, shown for context (inflation and risk mood); it does not count towards the verdict

The verdict compares those moves with gold's own move today:
CONFIRMS (the dollar and silver both back gold's direction), PARTLY, MIXED, or CONFLICTS.
This is a cross-check, not a trading signal.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from backend.data.base import DataUnavailableError
from backend.data.candles import TTLCache
from backend.data.http import HttpGet, make_http_get
from backend.timeutils import ist_label

BASE_URL = "https://api.twelvedata.com"
CACHE_SECONDS = 60
GOLD = "XAU/USD"

# symbol, display name, group, effect on gold when this symbol RISES (+1 good, -1 bad, 0 context only)
MARKETS: list[tuple[str, str, str, int]] = [
    ("XAU/USD", "Gold", "Gold", 0),
    ("XAG/USD", "Silver", "Commodities", +1),
    ("WTI/USD", "Crude oil (WTI)", "Commodities", 0),
    ("EUR/USD", "Euro / Dollar", "Forex", +1),
    ("GBP/USD", "Pound / Dollar", "Forex", +1),
    ("USD/JPY", "Dollar / Yen", "Forex", -1),
]
DOLLAR_PAIRS = {"EUR/USD": -1, "GBP/USD": -1, "USD/JPY": +1}   # sign that turns the pair's move into "dollar stronger"
FLAT_GOLD_PCT = 0.15       # gold moving less than this today = no clear direction
FLAT_DOLLAR_PCT = 0.08     # dollar gauge moving less than this = neutral
FLAT_SILVER_PCT = 0.25


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _direction(pct: float | None, flat: float) -> int:
    if pct is None:
        return 0
    return 1 if pct > flat else -1 if pct < -flat else 0


class RelatedMarkets:
    def __init__(self, api_key: str | None, http_get: HttpGet | None = None, clock: Callable[[], float] = time.time):
        self._key = api_key
        self._get = http_get or make_http_get(15.0)
        self._clock = clock
        self._cache = TTLCache(clock)

    def payload(self) -> dict:
        if not self._key:
            raise DataUnavailableError("Related markets need the Twelve Data key (TWELVEDATA_API_KEY).")
        return self._cache.get_or_load("related", CACHE_SECONDS, self._load)

    # ------------------------------------------------------------------ #
    def _load(self) -> dict:
        symbols = ",".join(s for s, *_ in MARKETS)
        body = self._get(f"{BASE_URL}/quote", {"symbol": symbols, "apikey": self._key})
        if not isinstance(body, dict):
            raise DataUnavailableError("Twelve Data related-markets quote: unexpected response format.")
        if body.get("status") == "error" and "code" in body:
            raise DataUnavailableError(f"Twelve Data related markets error {body.get('code')}: {body.get('message', '')}")

        rows = []
        for sym, name, group, effect in MARKETS:
            q = body.get(sym)
            if not isinstance(q, dict) or q.get("status") == "error" or _f(q.get("close")) is None:
                msg = q.get("message", "no data") if isinstance(q, dict) else "no data"
                rows.append({"symbol": sym, "name": name, "group": group, "available": False,
                             "note": "Not available on your plan" if isinstance(q, dict) and q.get("code") in (403, 404) else str(msg)[:120]})
                continue
            price, prev = _f(q.get("close")), _f(q.get("previous_close"))
            pct = _f(q.get("percent_change"))
            if pct is None and price is not None and prev:
                pct = (price - prev) / prev * 100
            move = _direction(pct, FLAT_SILVER_PCT if sym == "XAG/USD" else FLAT_DOLLAR_PCT if sym in DOLLAR_PAIRS else 0.25)
            rows.append({
                "symbol": sym, "name": name, "group": group, "available": True,
                "price": price, "change_pct": round(pct, 3) if pct is not None else None,
                "gold_effect": effect * move if effect else 0,
                "is_market_open": bool(q.get("is_market_open")),
            })
        by = {r["symbol"]: r for r in rows}
        gold_pct = by.get(GOLD, {}).get("change_pct")

        fx = [(-1 if DOLLAR_PAIRS[s] < 0 else 1) * by[s]["change_pct"] for s in DOLLAR_PAIRS
              if by.get(s, {}).get("available") and by[s]["change_pct"] is not None]
        dollar_pct = round(sum(fx) / len(fx), 3) if fx else None
        silver_pct = by.get("XAG/USD", {}).get("change_pct") if by.get("XAG/USD", {}).get("available") else None
        return {
            "source": "Twelve Data (same plan as the gold feed)",
            "fetched_at_ist": ist_label(datetime.fromtimestamp(self._clock(), tz=timezone.utc)),
            "gold_change_pct": gold_pct,
            "dollar_change_pct": dollar_pct,
            "silver_change_pct": silver_pct,
            "verdict": self._verdict(gold_pct, dollar_pct, silver_pct),
            "markets": rows,
            "method": "Changes are against each market's previous daily close. Dollar gauge = average of EUR/USD and GBP/USD "
                      "(inverted) and USD/JPY. Oil is shown for context only.",
        }

    @staticmethod
    def _verdict(gold_pct, dollar_pct, silver_pct) -> dict:
        g = _direction(gold_pct, FLAT_GOLD_PCT)
        if gold_pct is None:
            return {"state": "UNKNOWN", "text": "Gold's change today is not available."}
        if g == 0:
            return {"state": "FLAT", "text": f"Gold is nearly flat today ({gold_pct:+.2f}%), so there is no move to confirm."}
        word = "DOWN" if g < 0 else "UP"
        # what each market says about gold: dollar stronger -> gold down; silver down -> gold down
        votes, notes = [], []
        d = _direction(dollar_pct, FLAT_DOLLAR_PCT)
        if dollar_pct is not None:
            votes.append(-d)
            notes.append(f"US dollar {'stronger' if d > 0 else 'weaker' if d < 0 else 'flat'} ({dollar_pct:+.2f}%)")
        s = _direction(silver_pct, FLAT_SILVER_PCT)
        if silver_pct is not None:
            votes.append(s)
            notes.append(f"silver {'up' if s > 0 else 'down' if s < 0 else 'flat'} ({silver_pct:+.2f}%)")
        agree = sum(1 for v in votes if v == g)
        against = sum(1 for v in votes if v == -g)
        if not votes:
            state = "UNKNOWN"
        elif agree == len(votes):
            state = "CONFIRMS"
        elif against == len(votes):
            state = "CONFLICTS"
        elif agree and not against:
            state = "PARTLY"
        else:
            state = "MIXED"
        lead = {
            "CONFIRMS": f"Gold {word} ({gold_pct:+.2f}%) is confirmed by other markets",
            "PARTLY": f"Gold {word} ({gold_pct:+.2f}%) is partly confirmed",
            "MIXED": f"Gold {word} ({gold_pct:+.2f}%) – other markets are mixed",
            "CONFLICTS": f"Gold {word} ({gold_pct:+.2f}%) but other markets point the other way – the move may not last",
            "UNKNOWN": f"Gold {word} ({gold_pct:+.2f}%) – related markets unavailable",
        }[state]
        return {"state": state, "direction": word, "text": lead + (": " + ", ".join(notes) + "." if notes else ".")}
