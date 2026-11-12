from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from backend.analysis.news_scoring import build_news, classify, dedupe
from backend.data.base import DataUnavailableError
from backend.data.news_provider import FED_RSS, NewsItem, NewsProvider, parse_rss

NOW = datetime(2026, 11, 11, 14, 40, tzinfo=timezone.utc)


def gn_item(title, source, minutes_ago, link):
    pub = format_datetime(NOW - timedelta(minutes=minutes_ago))
    return (f"<item><title>{title} - {source}</title><link>{link}</link><pubDate>{pub}</pubDate>"
            f"<description>&lt;a href=\"{link}\"&gt;{title}&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color=\"#6f6f6f\"&gt;{source}&lt;/font&gt;</description>"
            f"<source url=\"https://x\">{source}</source></item>")


def rss(*items):
    return "<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>" + "".join(items) + "</channel></rss>"


GN = rss(
    gn_item("Gold rises as dollar slips after soft US jobs data", "Reuters", 20, "https://n/1"),
    gn_item("Gold rises as dollar slips after soft U.S. jobs data", "CNBC", 25, "https://n/2"),   # duplicate story
    gn_item("Fed signals higher for longer as inflation stays sticky", "Bloomberg", 90, "https://n/3"),
    gn_item("Gold prices fall 1% in Asian trade", "Kitco", 60, "https://n/4"),
    gn_item("Analysts expect central banks to keep buying gold in 2027", "FT", 300, "https://n/5"),
)
FED = rss(f"<item><title>Federal Reserve issues FOMC statement</title><link>https://fed/1</link>"
          f"<pubDate>{format_datetime(NOW - timedelta(minutes=10))}</pubDate><description>The Committee decided to lower the target range.</description></item>")


def test_parse_google_news_cleans_title_and_summary():
    items = parse_rss(GN, "Gold market")
    assert items[0].title == "Gold rises as dollar slips after soft US jobs data"
    assert items[0].source == "Reuters" and items[0].summary == ""
    assert items[0].published_utc.tzinfo is not None


def test_parse_fed_feed_uses_default_source():
    it = parse_rss(FED, "Fed", "Federal Reserve")[0]
    assert it.source == "Federal Reserve" and "lower the target range" in it.summary


def test_bad_xml_is_unavailable():
    with pytest.raises(DataUnavailableError):
        parse_rss("<html>blocked</html", "x")


def test_dedupe_merges_same_story():
    groups = dedupe(parse_rss(GN, "Gold market"))
    first = next(g for g in groups if "jobs" in g["item"].title)
    assert set(first["sources"]) == {"Reuters", "CNBC"}
    assert len(groups) == 4


def test_classification_rules():
    mk = lambda t, s="Reuters": NewsItem(t, s, NOW, "l", "", "f")  # noqa: E731
    dovish = classify(mk("Fed cuts rates by 25 basis points"))
    assert dovish["sentiment"] == "STRONGLY BULLISH" and dovish["impact"] == "High"
    hawk = classify(mk("Fed signals higher for longer as inflation stays sticky"))
    assert hawk["sentiment_score"] < 0 and hawk["impact"] == "High"
    react = classify(mk("Gold prices fall 1% in Asian trade"))
    assert react["type"] == "MARKET REACTION" and react["impact"] == "Low"
    opinion = classify(mk("Analysts expect central banks to keep buying gold"))
    assert opinion["type"] == "FORECAST / OPINION"
    official = classify(mk("Federal Reserve issues FOMC statement", "Federal Reserve"))
    assert official["type"] == "OFFICIAL RELEASE"
    nothing = classify(mk("Jewellery shop opens in Mumbai"))
    assert nothing["sentiment"] == "NEUTRAL" and "No clear gold driver" in nothing["explanation"]


def test_build_news_aggregate_excludes_reactions_and_duplicates():
    items = parse_rss(GN, "Gold market") + parse_rss(FED, "Fed", "Federal Reserve")
    n = build_news(items, NOW)
    fs = n["fundamental_sentiment"]
    titles = [x["title"] for x in n["items"]]
    assert sum("jobs" in t for t in titles) == 1                     # duplicate counted once
    assert fs["stories_counted"] == 4                                 # 5 unique − 1 market reaction
    jobs = next(x for x in n["items"] if "jobs" in x["title"])
    assert jobs["also_reported_by"] == ["CNBC"] and jobs["published_ist"].endswith("IST")
    assert any("FOMC" in h["title"] for h in n["recent_high_impact"])  # 10 min ago, High impact


def test_provider_survives_one_failing_feed():
    calls = []

    def fake_get(url):
        calls.append(url)
        if url == FED_RSS:
            raise DataUnavailableError("HTTP 503")
        return GN

    items, errors = NewsProvider(http_text=fake_get).fetch()
    assert items and "Federal Reserve (official)" in errors


def test_provider_all_fail_raises():
    def fail(url):
        raise DataUnavailableError("down")
    with pytest.raises(DataUnavailableError, match="No news feed"):
        NewsProvider(http_text=fail).fetch()


def test_fast_and_slow_feed_schedule_and_last_good_copy():
    from backend.data import news_provider as npv
    t = [1000.0]
    calls = []
    state = {"fail": False}

    def get(url):
        calls.append(url)
        if state["fail"]:
            raise DataUnavailableError("HTTP 429")
        return GN

    p = NewsProvider(http_text=get, clock=lambda: t[0])
    p.fetch()
    first = len(calls)
    assert first == len(npv.QUERIES) + 1                      # every feed once
    t[0] += 31
    p.fetch()
    assert len(calls) - first == len(npv.FAST_FEEDS)          # only fast feeds after 30 s
    t[0] += 100                                               # 131 s after start
    state["fail"] = True
    items, errors = p.fetch()
    assert items                                              # last good copies kept
    assert all("last good copy" in e for e in errors.values())


# ---- real headlines seen on 11 Nov 2026 (from the feed check on Karthik's PC)
@pytest.mark.parametrize("title,source,want_sign,want_type", [
    ("Gold struggles below $4,050 as Fed rate-hike bets intensify", "FXStreet", -1, None),
    ("Continuum Economic Chart USD Index DXY Update: Choppy trade turning higher", "Continuum", -1, None),
    ("Why US job market remains resilient despite wave of AI-driven layoffs", "X", -1, "FORECAST / OPINION"),
    ("COMMENTARY: A 50 bps rate hike? Warsh's Fed has a comms problem", "Reuters", -1, "FORECAST / OPINION"),
    ("One more rate hike is looking like the beginning, not the end", "Livemint", -1, "FORECAST / OPINION"),
    ("Gold Faces Fresh Headwinds as Real Yields Climb After US CPI", "FXEmpire", -1, "NEWS REPORT"),
])
def test_real_headlines(title, source, want_sign, want_type):
    c = classify(NewsItem(title, source, NOW, "l", "", "f"))
    assert (c["sentiment_score"] > 0) - (c["sentiment_score"] < 0) == want_sign, c
    if want_type:
        assert c["type"] == want_type
    assert c["relevant"]


@pytest.mark.parametrize("title", [
    "Grade Control Drilling Begins at a Permitted Tanzanian Gold Project Before Initial Production",
    "Gold Miners: The Opportunity Behind The Volatility",
    "Dynacor Reports Q3-2026 Results with Record Production and Progress at Galam",
])
def test_mining_company_news_is_not_gold_price_news(title):
    assert classify(NewsItem(title, "X", NOW, "l", "", "f"))["relevant"] is False


def test_price_of_gold_story_is_relevant():
    assert classify(NewsItem("Current price of gold as of November 11, 2026", "Fortune", NOW, "l", "", "f"))["relevant"]


def test_fed_non_policy_release_is_low_impact():
    c = classify(NewsItem("Federal Reserve Board announces approval of application by First Bank", "Federal Reserve", NOW, "l", "", "f"))
    assert c["impact"] == "Low" and c["type"] == "OFFICIAL RELEASE"
    c2 = classify(NewsItem("Federal Reserve issues FOMC statement", "Federal Reserve", NOW, "l", "", "f"))
    assert c2["impact"] == "High"


def test_fed_feed_with_byte_order_mark_and_html_block():
    bom = b"\xef\xbb\xbf" + FED.encode("utf-8")
    assert parse_rss(bom, "Fed", "Federal Reserve")[0].source == "Federal Reserve"
    with pytest.raises(DataUnavailableError, match="web page instead of a feed"):
        parse_rss(b"<!DOCTYPE html><html><body>Access denied</body></html>", "Fed")


def test_mixed_headline_with_hawkish_drivers_scores_bearish():
    c = classify(NewsItem("Gold edges above $4,050 as firmer Fed rate bets curb gains, Dollar holds highs", "VT Markets", NOW, "l", "", "f"))
    assert c["sentiment_score"] < 0 and c["type"] == "NEWS REPORT"


def test_dollar_holds_highs_is_bearish_for_gold():
    c = classify(NewsItem("Dollar holds highs ahead of Fed speakers", "X", NOW, "l", "", "f"))
    assert c["sentiment_score"] < 0


def test_bank_forecast_headline_is_bullish_opinion():
    c = classify(NewsItem("Gold prices find solid support at $4,000 as Standard Chartered sees rally to $4,650 by year-end", "Kitco", NOW, "l", "", "f"))
    assert c["sentiment_score"] > 0 and c["type"] == "FORECAST / OPINION"


def test_crypto_story_is_not_gold_news():
    c = classify(NewsItem("Bitcoin slips below $74,000, Ethereum falls to $2,053 as US inflation, rising Treasury yields and ETF outflows weigh", "The Economic Times", NOW, "l", "", "f"))
    assert c["relevant"] is False


def test_rising_treasury_yields_phrase_is_bearish():
    c = classify(NewsItem("Gold under pressure from rising Treasury yields", "X", NOW, "l", "", "f"))
    assert c["sentiment_score"] < 0
