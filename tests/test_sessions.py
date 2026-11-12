from datetime import datetime, timezone

import numpy as np
import pandas as pd

from backend.analysis.sessions import SESSIONS, build_sessions, latest_window

UTC = timezone.utc


def _times(key, now):
    _, _, tz, o, c = next(s for s in SESSIONS if s[0] == key)
    return latest_window(tz, o, c, now)


def test_winter_session_hours_in_utc():
    now = datetime(2026, 11, 11, 23, 0, tzinfo=UTC)      # Wednesday, no DST in London/NY
    assert _times("ASIAN", now)[0] == datetime(2026, 11, 11, 0, 0, tzinfo=UTC)    # 05:30 IST
    assert _times("LONDON", now)[0] == datetime(2026, 11, 11, 8, 0, tzinfo=UTC)   # 13:30 IST
    assert _times("NEW_YORK", now)[0] == datetime(2026, 11, 11, 13, 0, tzinfo=UTC)  # 18:30 IST


def test_summer_session_hours_shift_with_dst():
    now = datetime(2026, 7, 15, 23, 0, tzinfo=UTC)
    assert _times("LONDON", now)[0] == datetime(2026, 7, 15, 7, 0, tzinfo=UTC)
    assert _times("NEW_YORK", now)[0] == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert _times("ASIAN", now)[0] == datetime(2026, 7, 15, 0, 0, tzinfo=UTC)     # Tokyo has no DST


def test_weekend_uses_fridays_session():
    sat = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    start, _ = _times("LONDON", sat)
    assert start.date().isoformat() == "2026-11-13"


def test_session_stats_from_candles():
    idx = pd.date_range("2026-11-11 00:00", "2026-11-11 14:30", freq="5min", tz="UTC")
    close = 4000 + np.arange(len(idx)) * 0.5
    m5 = pd.DataFrame({"open": close - 0.2, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0}, index=idx)
    now = datetime(2026, 11, 11, 14, 40, tzinfo=UTC)
    out = {s["key"]: s for s in build_sessions(m5, now, data_until=idx[-1].to_pydatetime())}
    asian, london, ny = out["ASIAN"], out["LONDON"], out["NEW_YORK"]
    assert asian["status"] == "CLOSED" and london["status"] == "OPEN" and ny["status"] == "OPEN"
    assert asian["candles"] == 108                     # 00:00–08:55 UTC
    assert asian["open"] == round(close[0] - 0.2, 2) and asian["high"] == round(close[107] + 1, 2)
    assert asian["direction"] == "UP"
    assert ny["start_ist"] == "11 Nov 2026, 18:30:00 IST"
    assert ny["data_until_ist"] == "11 Nov 2026, 20:00:00 IST"


def test_session_without_data_says_so():
    idx = pd.date_range("2026-11-10 00:00", periods=10, freq="5min", tz="UTC")
    m5 = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
    out = build_sessions(m5, datetime(2026, 11, 11, 14, 0, tzinfo=UTC), None)
    assert all(s["has_data"] is False for s in out)
