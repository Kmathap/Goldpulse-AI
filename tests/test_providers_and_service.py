from dataclasses import replace

import pandas as pd
import pytest

from backend.config import Settings
from backend.data.base import DataUnavailableError
from backend.data.candles import parse_time, resample_h4
from backend.data.http import _safe
from backend.data.market_service import MarketService
from backend.data.twelvedata_provider import TwelveDataProvider
from backend.data.xaus_provider import XausProvider
from fakes import NOW, Clock, FakeTwelveData, FakeXaus


def xaus_env(**fake_kw):
    clock = Clock()
    fake = FakeXaus(clock, **fake_kw)
    settings = Settings()
    provider = XausProvider(settings, http_get=fake, clock=clock)
    return settings, clock, fake, provider, MarketService(provider, settings, clock=clock)


# ------------------------------------------------------------- parsing
def test_parse_time_formats():
    s = parse_time(1_700_000_000)
    assert parse_time(1_700_000_000_000) == s
    assert parse_time("1700000000") == s
    assert parse_time("2026-11-11 13:00:00").isoformat() == "2026-11-11T13:00:00+00:00"
    assert parse_time("2026-11-11T13:00:00Z").isoformat() == "2026-11-11T13:00:00+00:00"
    with pytest.raises(DataUnavailableError):
        parse_time(None)


def test_resample_h4_from_h1():
    idx = pd.date_range("2026-11-10 00:00", periods=8, freq="h", tz="UTC")
    h1 = pd.DataFrame({"open": range(8), "high": [x + 2 for x in range(8)], "low": range(8),
                       "close": [x + 1 for x in range(8)], "volume": 1.0}, index=idx, dtype=float)
    h4 = resample_h4(h1)
    assert list(h4.index.hour) == [0, 4]
    assert h4.iloc[0].tolist() == [0, 5, 0, 4, 4]


def test_secret_masked_in_errors():
    assert "SECRET" not in _safe("https://x", {"apikey": "SECRET", "symbol": "XAU/USD"})


# ------------------------------------------------------------- xaus provider
def test_xaus_quote_and_cache():
    _, clock, fake, provider, _ = xaus_env()
    q = provider.get_quote()
    assert q.price == 4150.40 and q.source_state == "fresh"
    provider.get_quote()
    assert sum(1 for u, _ in fake.calls if u.endswith("/spot")) == 1        # cached
    clock.t += 61
    provider.get_quote()
    assert sum(1 for u, _ in fake.calls if u.endswith("/spot")) == 2


def test_xaus_chart_params_and_h4():
    _, _, fake, provider, _ = xaus_env()
    m5 = provider.get_candles("M5")
    assert len(m5.df) == 600 and str(m5.df.index.tz) == "UTC"
    assert fake.calls[-1][1] == {"symbol": "xau", "interval": "5m", "range": "5d"}
    h4 = provider.get_candles("H4")
    assert (h4.df.index.hour % 4 == 0).all()
    assert any("H4 built from H1" in n for n in h4.notes)


def test_xaus_millisecond_timestamps():
    _, _, fake, provider, _ = xaus_env()
    fake.ms_timestamps = True
    assert provider.get_candles("M15").df.index[-1].year == 2026


def test_xaus_unavailable_spot_raises():
    _, _, fake, provider, _ = xaus_env()
    fake.spot_state = "unavailable"
    with pytest.raises(DataUnavailableError):
        provider.get_quote()


def test_xaus_unexpected_format_is_reported():
    clock = Clock()
    provider = XausProvider(Settings(), http_get=lambda u, p=None: {"weird": 1}, clock=clock)
    with pytest.raises(DataUnavailableError, match="unexpected format"):
        provider.get_candles("M5")


# ------------------------------------------------------------- twelve data
def test_twelvedata_provider():
    clock = Clock()
    fake = FakeTwelveData(clock)
    p = TwelveDataProvider(Settings(twelvedata_api_key="K"), http_get=fake, clock=clock)
    q = p.get_quote()
    assert q.price == 4150.40 and (NOW - q.time_utc.timestamp()) == pytest.approx(5)
    for tf in ("M5", "H4", "D1"):
        assert len(p.get_candles(tf).df) == 600
    assert fake.calls[-1][1]["interval"] == "1day" and fake.calls[-1][1]["timezone"] == "UTC"


def test_twelvedata_plan_error_message():
    clock = Clock()
    fake = FakeTwelveData(clock)
    fake.error = {"code": 403, "message": "This symbol is available starting with the Grow plan", "status": "error"}
    p = TwelveDataProvider(Settings(twelvedata_api_key="K"), http_get=fake, clock=clock)
    with pytest.raises(DataUnavailableError, match="Grow plan"):
        p.get_candles("H1")


def test_auto_provider_selection():
    assert Settings().resolved_provider == "xaus"
    assert Settings(twelvedata_api_key="K").resolved_provider == "twelvedata"


# ------------------------------------------------------------- service
def test_snapshot_live():
    _, _, fake, _, service = xaus_env()
    snap = service.price_snapshot()
    assert snap["status"] == "LIVE", snap["status_reason"]
    assert snap["price"] == 4150.40 and snap["spot"]["source"].startswith("xaus.com spot")
    assert snap["chart"]["basis_vs_spot"] == 0.0 and snap["warnings"] == []
    assert snap["previous_close"] is not None and snap["change"] is not None
    assert snap["daily_high"] >= snap["chart"]["price"] >= snap["daily_low"]
    assert snap["volatility"]["regime"] in {"LOW", "NORMAL", "HIGH"}
    assert snap["trend_direction"] is None
    assert snap["spot"]["time_ist"].endswith("IST")


def test_snapshot_warns_on_futures_like_gap():
    _, _, _, _, service = xaus_env(chart_shift=25.0)
    snap = service.price_snapshot()
    assert snap["chart"]["basis_vs_spot"] == 25.0
    assert any("futures" in w for w in snap["warnings"])


def test_snapshot_stale_spot():
    _, _, fake, _, service = xaus_env()
    fake.spot_age = 900
    snap = service.price_snapshot()
    assert snap["status"] == "STALE" and "spot quote" in snap["status_reason"].lower()


def test_snapshot_weekend_closed():
    _, clock, _, _, service = xaus_env()
    clock.t = NOW + 3 * 86400            # Saturday
    snap = service.price_snapshot()
    assert snap["status"] == "MARKET_CLOSED"


def test_snapshot_unavailable_has_no_numbers():
    _, _, fake, _, service = xaus_env()
    fake.fail = {"chart"}
    snap = service.price_snapshot()
    assert snap["status"] == "DATA_UNAVAILABLE" and "price" not in snap


def test_snapshot_without_spot_uses_chart_and_says_so():
    _, _, fake, _, service = xaus_env()
    fake.fail = {"spot"}
    snap = service.price_snapshot()
    assert snap["price_basis"].startswith("chart series") and snap["warnings"]


def test_technicals_exclude_forming_candle():
    _, _, _, _, service = xaus_env()
    t = service.technicals("H1")
    assert t["forming_candle_excluded"] is True and t["bars_used"] == 599
    for key in ("ema20", "ema50", "ema200", "rsi14", "macd", "adx14", "bb_upper", "atr14"):
        assert t[key] is not None, key


def test_technicals_all_timeframes():
    _, _, _, _, service = xaus_env()
    out = service.technicals_all()["timeframes"]
    assert set(out) == {"M5", "M15", "M30", "H1", "H4", "D1"}
    assert all(v["status"] == "LIVE" for v in out.values()), {k: v.get("status_reason") for k, v in out.items()}
    assert out["H4"]["ema200"] is None and out["H4"]["ema200_note"]   # only ~150 H4 bars from 600 H1


def test_candles_payload_marks_forming():
    _, _, _, _, service = xaus_env()
    p = service.candles_payload("M15", 5)
    assert p["count"] == 5 and p["candles"][-1]["forming"] is True
    assert p["candles"][-1]["time_ist"].endswith("+05:30")


def test_health():
    _, _, _, _, service = xaus_env()
    h = service.health()
    assert h["status"] == "OK" and h["checks"]["quote"]["ok"]


# ------------------------------------------------------------- real-feed quirks seen on 11 Nov 2026
def test_off_boundary_live_point_is_merged_not_a_candle():
    from backend.data.candles import align_to_interval
    idx = pd.to_datetime(["2026-11-11 13:55:00", "2026-11-11 14:00:00", "2026-11-11 14:04:40"], utc=True)
    df = pd.DataFrame({"open": [1.0, 2.0, 5.0], "high": [2.0, 3.0, 5.0], "low": [0.5, 1.5, 5.0],
                       "close": [1.5, 2.5, 5.0], "volume": [10.0, 10.0, 0.0]}, index=idx)
    out = align_to_interval(df, 300)
    assert list(out.index.strftime("%H:%M")) == ["13:55", "14:00"]
    assert out.iloc[-1].tolist() == [2.0, 5.0, 1.5, 5.0, 10.0]


def test_provider_aligns_and_records_last_point():
    _, clock, fake, provider, _ = xaus_env()
    fake.live_point_lag = 10 * 60 + 20          # newest point ~10 min old, off-boundary
    m5 = provider.get_candles("M5")
    assert (m5.df.index.second == 0).all() and (m5.df.index.minute % 5 == 0).all()
    # the 12:52:10 live point is merged into the 12:50 candle, not added as its own candle
    assert m5.df.index[-1].strftime("%H:%M") == "12:50" and m5.df.index.is_unique
    assert m5.df["close"].iloc[-1] == fake.spot_price
    assert clock() - m5.last_point_utc.timestamp() == pytest.approx(620)


def test_snapshot_reports_delayed_chart():
    _, _, fake, _, service = xaus_env()
    fake.live_point_lag = 11 * 60
    snap = service.price_snapshot()
    assert snap["status"] == "LIVE"              # 11 min < 30 min stale limit
    assert snap["chart"]["feed"].startswith("delayed") and snap["chart"]["delay_minutes"] == 11.0
    assert any("delayed by about 11 minutes" in w for w in snap["warnings"])


def test_snapshot_near_realtime_chart_has_no_delay_warning():
    _, _, _, _, service = xaus_env()
    snap = service.price_snapshot()
    assert snap["chart"]["feed"] == "near real-time"
    assert not any("delayed" in w for w in snap["warnings"])


def test_delayed_feed_partial_candle_is_not_treated_as_closed():
    # Seen live 11 Nov 2026: clock 14:30:53 UTC, data only up to 14:19:27 UTC.
    # The M30 14:00 candle "ends" at 14:30 by the clock but holds only 19 minutes of data.
    _, clock, fake, _, service = xaus_env()
    clock.t = clock.t - (clock.t % 1800) + 1800 + 53       # just after a 30-min boundary
    fake.live_point_lag = 11 * 60 + 26
    for tf in ("M5", "M15", "M30"):
        t = service.technicals(tf)
        assert t["forming_candle_excluded"] is True, tf


def test_sessions_payload_and_candles_with_ema():
    _, _, _, _, service = xaus_env()
    sp = service.sessions_payload()
    assert [s["key"] for s in sp["sessions"]] == ["ASIAN", "LONDON", "NEW_YORK"]
    assert all("start_ist" in s for s in sp["sessions"])
    p = service.candles_payload("H1", 50, with_ema=True)
    assert p["candles"][-2]["ema20"] is not None and p["candles"][-2]["ema200"] is not None
    assert p["candles"][-1]["forming"] is True and p["candles"][-1]["ema20"] is None


def test_analysis_endpoint_payload():
    _, _, _, _, service = xaus_env()
    a = service.analysis()
    assert a["status"] == "LIVE"
    assert a["verdict"]["direction"] in {"BULLISH", "BEARISH", "NEUTRAL"}
    assert a["plan"]["action"] in {"BUY", "SELL", "NO TRADE"}
    assert set(a["timeframes"]) == {"M5", "M15", "M30", "H1", "H4", "D1"}
    import json; json.dumps(a)                      # must be JSON-serialisable


def test_analysis_unavailable_without_data():
    _, _, fake, _, service = xaus_env()
    fake.fail = {"chart"}
    assert service.analysis()["status"] == "DATA_UNAVAILABLE"


def test_analysis_includes_news_and_blocks_on_fresh_high_impact_news():
    from backend.data.news_provider import NewsProvider
    from test_news import GN, FED, rss, gn_item
    from fakes import NOW as FNOW
    from datetime import datetime, timezone
    settings, clock, fake, provider, _ = xaus_env()
    # news timestamps relative to the fake clock
    import test_news
    test_news_now = datetime.fromtimestamp(FNOW, tz=timezone.utc)
    test_news.NOW = test_news_now
    feed = rss(gn_item("Fed cuts rates in surprise move", "Reuters", 5, "https://n/x"))
    svc = MarketService(provider, settings, clock=clock, news_provider=NewsProvider(http_text=lambda u: feed, clock=clock))
    a = svc.analysis()
    assert a["fundamental_sentiment"]["label"] != "NO DATA"
    assert any("HIGH-IMPACT NEWS" in x for x in a["plan"]["avoid_when"])
    assert svc.news_payload()["items"][0]["sentiment"] == "STRONGLY BULLISH"


def test_twelvedata_history_requests_5000_bars():
    clock = Clock()
    fake = FakeTwelveData(clock)
    p = TwelveDataProvider(Settings(twelvedata_api_key="K"), http_get=fake, clock=clock)
    p.get_history("H1")
    assert fake.calls[-1][1]["outputsize"] == 5000 and fake.calls[-1][1]["interval"] == "1h"
    p.get_history("H1", bars=99999)
    assert fake.calls[-1][1]["outputsize"] == 5000 or len(fake.calls) == 1   # capped (and cached)


def test_xaus_history_falls_back_to_live_series():
    _, _, _, provider, _ = xaus_env()
    assert len(provider.get_history("H1").df) == len(provider.get_candles("H1").df)
