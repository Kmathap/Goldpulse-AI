"""Rule-based gold-impact scoring for news headlines (free; no AI API needed).

Each item gets:
* impact on gold: strongly/moderately bullish, neutral, moderately/strongly bearish
* expected market impact: High / Medium / Low (by topic)
* type: official release / news report / forecast-opinion / market-reaction report
* the matched rules, explained in plain language

Same story from several publishers is merged (counted once). Headlines that only
describe gold's own price move ("gold falls 1%") are tagged as MARKET REACTION:
they describe what already happened and are excluded from the sentiment total,
so the actual reaction is kept separate from the expected (driver) impact.

The classification is keyword-based. It can misread complex headlines: always
open the article before acting.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable

from backend.data.news_provider import NewsItem
from backend.timeutils import iso, ist_label

# (pattern, score, explanation)
DRIVER_RULES: list[tuple[str, int, str]] = [
    (r"rate[- ]cuts?|cuts? (interest )?rates|dovish|easing cycle|pause (rate[- ])?hikes|lower rates",
     2, "Points to lower US interest rates – cheaper to hold non-yielding gold, usually supportive."),
    (r"rate[- ]hikes?|raises? (interest )?rates|hawkish|higher for longer|tighten|bps (rate )?hike|(firmer|rising|higher) (fed )?rate bets",
     -2, "Points to higher or longer-high US rates – raises the cost of holding gold, usually negative."),
    (r"(weaker|softer) dollar|(dollar|dxy|usd index)[^.]{0,40}\b(falls|slips|drops|weakens|declines|slides|eases|turn(s|ing)? lower|lower)\b",
     1, "Weaker US dollar – gold becomes cheaper for non-USD buyers, usually supportive."),
    (r"(stronger|firmer) dollar|(dollar|dxy|usd index)[^.]{0,40}\b(rises|gains|climbs|jumps|strengthens|firms|rallies|turn(s|ing)? higher|higher|holds (near )?highs?)\b",
     -1, "Stronger US dollar – usually a headwind for gold."),
    (r"yields? (fall|falls|drop|drops|slip|slips|decline|declines|ease|eases|retreat)|(falling|lower|easing) (treasury |bond )?yields",
     1, "Falling Treasury yields – lowers the opportunity cost of gold, usually supportive."),
    (r"yields? (rise|rises|climb|climbs|jump|jumps|surge|surges|spike|spikes)|(rising|higher|surging) (treasury |bond |real )?yields",
     -1, "Rising Treasury yields – raises the opportunity cost of gold, usually negative."),
    (r"(cpi|inflation|pce)\b.*\b(cool|cools|slows|eases|softer|lower than expected|below (forecast|expectations))",
     1, "Cooler inflation – supports rate-cut expectations, usually good for gold."),
    (r"(cpi|inflation|pce)\b.*\b(hot|hotter|accelerat|higher than expected|above (forecast|expectations)|sticky)",
     -1, "Hotter inflation – may keep the Fed hawkish, usually negative for gold in the short term."),
    (r"(payrolls|jobs|hiring|employment)\b.*\b(miss|weaker|weak|falls|slows|below)|unemployment (rises|rose|jumps|climbs)|jobless claims (rise|jump|climb)",
     1, "Weaker US jobs data – supports rate cuts, usually good for gold."),
    (r"(payrolls|jobs|hiring|employment)\b.*\b(beat|strong|surge|jumps|above)|unemployment (falls|fell|drops)|jobless claims (fall|drop)|(job|labou?r) market (remains |stays |is )?(resilient|strong|robust|tight)",
     -1, "Stronger US jobs data – reduces rate-cut odds, usually negative for gold."),
    (r"safe[- ]haven|geopolitic|\bwar\b|conflict|missile|airstrike|sanction|escalat|tensions (rise|mount|flare)",
     1, "Geopolitical risk – safe-haven demand, usually supportive."),
    (r"ceasefire|peace (deal|talks|agreement)|tensions ease|de-?escalat",
     -1, "Easing geopolitical risk – reduces safe-haven demand."),
    (r"central banks? (buy|buys|bought|add|adds|added|purchas)|(pboc|rbi|central bank).*gold (reserves|purchases|buying)",
     1, "Central-bank gold buying – structural demand, supportive."),
    (r"etf inflows?|inflows? (into|to) gold|gold etf holdings (rise|climb)",
     1, "Gold ETF inflows – investor demand rising."),
    (r"etf outflows?|outflows? from gold|gold etf holdings (fall|drop)",
     -1, "Gold ETF outflows – investor demand falling."),
    (r"(sees|forecasts?|targets?|expects?|predicts?)[^.]{0,40}\b(rally|rise|gains?|climb|surge|rebound)\b[^.]{0,20}\$|(raises?|lifts?|hikes?) (its |their )?gold (price )?(forecast|target)",
     1, "A bank or analyst forecast of higher gold prices (an opinion, not a fact)."),
    (r"(sees|forecasts?|targets?|expects?|predicts?)[^.]{0,40}\b(fall|drop|decline|slide)\b[^.]{0,20}\$|(cuts?|lowers?) (its |their )?gold (price )?(forecast|target)",
     -1, "A bank or analyst forecast of lower gold prices (an opinion, not a fact)."),
    (r"recession|slowdown|uncertainty|stimulus",
     1, "Economic uncertainty or stimulus – often supportive for gold."),
]
REACTION_RULES: list[tuple[str, int, str]] = [
    (r"gold (prices? )?(rise|rises|gain|gains|climb|climbs|jump|jumps|rall|surge|hits? (a )?record|record high|edges? (up|higher))",
     1, "Describes gold already rising (market reaction)."),
    (r"gold (prices? )?(fall|falls|drop|drops|slip|slips|decline|declines|slide|slides|tumble|plunge|edges? (down|lower)|retreat)",
     -1, "Describes gold already falling (market reaction)."),
]
TOPICS: list[tuple[str, str, str]] = [
    # topic, pattern, impact
    ("Fed / rates", r"federal reserve|\bfed\b|fomc|powell|interest rate|rate (cut|hike)", "High"),
    ("Inflation", r"\bcpi\b|\bpce\b|inflation", "High"),
    ("Jobs", r"payroll|nonfarm|jobless|unemployment|jobs report", "High"),
    ("GDP", r"\bgdp\b", "High"),
    ("Dollar", r"dollar|\bdxy\b", "Medium"),
    ("Yields", r"yield|treasur", "Medium"),
    ("Geopolitics", r"geopolitic|\bwar\b|conflict|missile|sanction|tension|ceasefire", "Medium"),
    ("Central banks", r"central bank|pboc|reserves", "Medium"),
    ("Gold ETFs", r"\betf\b", "Medium"),
    ("China", r"china|chinese|pboc|beijing", "Medium"),
]
OPINION = re.compile(
    r"\b(expects?|expected|forecasts?|could|may|might|predicts?|outlook|analysts?|strategists?|poll|survey|sees|targets?|"
    r"commentary|opinion|analysis|column|explainer|looks? like|looking like)\b|\?|^(why|how|what|will|is|can)\b",
    re.I,
)
CRYPTO = re.compile(r"bitcoin|ethereum|crypto|\bbtc\b|\beth\b|solana|xrp|altcoin", re.I)
MONETARY = re.compile(r"fomc|monetary policy|federal funds|interest rate|discount rate|minutes of the|policy statement|economic projections|balance sheet|rate decision", re.I)
# A story is kept only if it is about the gold price or one of its drivers.
GOLD_PRICE = re.compile(r"gold (price|prices|futures)|price of gold|spot gold|bullion|xau|precious metals|gold (rises|falls|gains|drops|struggles|slips|climbs|edges|hits|holds|steadies|eases|jumps|rallies|tumbles)", re.I)
LABELS = {2: "STRONGLY BULLISH", 1: "MODERATELY BULLISH", 0: "NEUTRAL", -1: "MODERATELY BEARISH", -2: "STRONGLY BEARISH"}
IMPACT_WEIGHT = {"High": 3, "Medium": 2, "Low": 1}


def _tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2}


def dedupe(items: Iterable[NewsItem], threshold: float = 0.55) -> list[dict]:
    """Merge near-identical headlines (same story, different publishers or feeds)."""
    groups: list[dict] = []
    for it in sorted(items, key=lambda x: x.published_utc, reverse=True):
        tk = _tokens(it.title)
        for g in groups:
            if g["link"] == it.link or (tk and len(tk & g["tokens"]) / len(tk | g["tokens"]) >= threshold):
                if it.source not in g["sources"]:
                    g["sources"].append(it.source)
                g["feeds"].add(it.feed)
                break
        else:
            groups.append({"item": it, "tokens": tk, "link": it.link, "sources": [it.source], "feeds": {it.feed}})
    return groups


def classify(it: NewsItem) -> dict:
    text = f"{it.title}. {it.summary}".lower()
    drivers = [(s, e) for p, s, e in DRIVER_RULES if re.search(p, text)]
    reactions = [(s, e) for p, s, e in REACTION_RULES if re.search(p, text)]
    topics = [(t, imp) for t, p, imp in TOPICS if re.search(p, text)]
    impact = "High" if any(i == "High" for _, i in topics) else "Medium" if topics else "Low"

    if it.source.lower().startswith("federal reserve"):
        kind = "OFFICIAL RELEASE"
    elif OPINION.search(it.title):
        kind = "FORECAST / OPINION"
    elif drivers:
        kind = "NEWS REPORT"
    elif reactions:
        kind = "MARKET REACTION"
    else:
        kind = "NEWS REPORT"
    relevant = bool(drivers or reactions or topics or GOLD_PRICE.search(text) or kind == "OFFICIAL RELEASE")
    if CRYPTO.search(it.title) and not re.search(r"\bgold\b|bullion|xau", it.title, re.I):
        relevant = False      # crypto market stories that merely mention macro drivers
    if kind == "OFFICIAL RELEASE" and not MONETARY.search(text):
        # bank approvals, enforcement actions, etc. – not market-moving for gold
        topics, drivers, reactions, impact = [("Fed (non-policy)", "Low")], [], [], "Low"

    if drivers:
        raw = sum(s for s, _ in drivers)
        explanation = " ".join(e for _, e in drivers)
    elif reactions:
        raw = sum(s for s, _ in reactions)
        explanation = " ".join(e for _, e in reactions) + " Reported price moves are not counted as new drivers."
        impact = "Low"
    else:
        raw, explanation = 0, "No clear gold driver found in the headline – open the article to judge."
    score = max(-2, min(2, raw))
    return {
        "sentiment_score": score,
        "sentiment": LABELS[score],
        "impact": impact,
        "type": kind,
        "topics": [t for t, _ in topics],
        "explanation": explanation,
        "relevant": relevant,
    }


def build_news(items: list[NewsItem], now: datetime, max_items: int = 40, max_age_hours: int = 72) -> dict:
    cutoff = now - timedelta(hours=max_age_hours)
    groups = [g for g in dedupe(items) if g["item"].published_utc >= cutoff]
    out = []
    hidden = 0
    agg_num = agg_den = 0.0
    recent_high = []
    for g in groups:
        it: NewsItem = g["item"]
        c = classify(it)
        if not c["relevant"]:
            hidden += 1
            continue
        if len(out) >= max_items:
            break
        age_h = (now - it.published_utc).total_seconds() / 3600
        if c["type"] != "MARKET REACTION" and age_h <= 24:
            w = IMPACT_WEIGHT[c["impact"]] * (1.0 if age_h <= 6 else 0.6)
            agg_num += w * c["sentiment_score"]
            agg_den += w
        if c["impact"] == "High" and 0 <= age_h <= 0.5 and c["type"] in ("NEWS REPORT", "OFFICIAL RELEASE"):
            recent_high.append({"title": it.title, "minutes_ago": round(age_h * 60)})
        out.append(
            {
                "title": it.title,
                "source": it.source,
                "also_reported_by": g["sources"][1:],
                "published_utc": iso(it.published_utc),
                "published_ist": ist_label(it.published_utc),
                "age_minutes": round(age_h * 60),
                "summary": it.summary,
                "link": it.link,
                "feeds": sorted(g["feeds"]),
                **c,
            }
        )
    avg = agg_num / agg_den if agg_den else 0.0
    if not agg_den:
        label = "NO DATA"
    elif avg >= 1.0:
        label = "STRONGLY BULLISH"
    elif avg >= 0.3:
        label = "MODERATELY BULLISH"
    elif avg <= -1.0:
        label = "STRONGLY BEARISH"
    elif avg <= -0.3:
        label = "MODERATELY BEARISH"
    else:
        label = "NEUTRAL / MIXED"
    counted = [x for x in out if x["type"] != "MARKET REACTION" and x["age_minutes"] <= 24 * 60]
    return {
        "items": out,
        "fundamental_sentiment": {
            "label": label,
            "score": round(avg, 2),
            "stories_counted": len(counted),
            "bullish": sum(1 for x in counted if x["sentiment_score"] > 0),
            "bearish": sum(1 for x in counted if x["sentiment_score"] < 0),
            "neutral": sum(1 for x in counted if x["sentiment_score"] == 0),
            "method": "Last 24 h, unique stories only, market-reaction headlines excluded, weighted by impact (High 3, Medium 2, Low 1) and recency.",
        },
        "recent_high_impact": recent_high,
        "hidden_not_gold_related": hidden,
        "classifier": "Rule-based keyword classifier (free). It can misread complex headlines – open the article before acting.",
    }
