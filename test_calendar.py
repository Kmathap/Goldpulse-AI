from datetime import datetime, timedelta, timezone

from backend.data.base import DataUnavailableError
from backend.data.calendar_provider import CACHE_SECONDS, RETRY_AFTER_FAIL, CalendarProvider
from fakes import NOW, Clock

NY = timezone(timedelta(hours=-5))


def ev(title, minutes, impact="High", country="USD"):
    t = datetime.fromtimestamp(NOW, tz=timezone.utc) + timedelta(minutes=minutes)
    return {"title": title, "country": country, "date": t.astimezone(NY).isoformat(), "impact": impact,
            "forecast": "0.3%", "previous": "0.2%"}


class FeedStub:
    def __init__(self, body):
        self.body, self.calls = body, 0

    def __call__(self, url, params=None):
        self.calls += 1
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def make(body, clock=None):
    stub = FeedStub(body)
    return CalendarProvider(http_get=stub, clock=clock or Clock()), stub


def test_no_trade_when_high_usd_event_is_close():
    cal, _ = make([ev("CPI m/m", 20), ev("German ZEW", 5, country="EUR")])
    p = cal.payload()
    assert p["warning"]["state"] == "NO_TRADE"
    assert "CPI" in p["warning"]["text"]
    assert [e["title"] for e in p["events"]] == ["CPI m/m"]          # non-USD filtered out
    assert p["events"][0]["key_event"] is True
    assert p["events"][0]["minutes_until"] == 20


def test_no_trade_shortly_after_event():
    cal, _ = make([ev("Non-Farm Employment Change", -10)])
    assert cal.payload()["warning"]["state"] == "NO_TRADE"


def test_caution_and_clear():
    cal, _ = make([ev("FOMC Statement", 90)])
    assert cal.payload()["warning"]["state"] == "CAUTION"
    cal, _ = make([ev("FOMC Statement", 300), ev("Existing Home Sales", 10, impact="Medium")])
    p = cal.payload()
    assert p["warning"]["state"] == "CLEAR"
    assert "FOMC Statement" in p["warning"]["text"]
    assert len(p["events"]) == 2


def test_low_impact_and_old_events_hidden():
    cal, _ = make([ev("Some Low", 30, impact="Low"), ev("Old CPI", -400)])
    p = cal.payload()
    assert p["events"] == []
    assert p["warning"]["state"] == "CLEAR"


def test_cached_for_an_hour_and_keeps_last_copy_on_failure():
    clock = Clock()
    cal, stub = make([ev("CPI m/m", 300)], clock)
    cal.payload(); cal.payload()
    assert stub.calls == 1
    clock.t += CACHE_SECONDS + 1
    stub.body = DataUnavailableError("feed down")
    p = cal.payload()
    assert stub.calls == 2 and p["stale_note"] and p["events"]
    cal.payload()
    assert stub.calls == 2                       # waits RETRY_AFTER_FAIL before trying again
    clock.t += RETRY_AFTER_FAIL + 1
    cal.payload()
    assert stub.calls == 3


def test_unavailable_when_never_loaded():
    cal, _ = make(DataUnavailableError("no network"))
    try:
        cal.payload()
        assert False, "should raise"
    except DataUnavailableError as exc:
        assert "no network" in str(exc)


def test_bad_format():
    cal, _ = make({"error": "x"})
    try:
        cal.payload()
        assert False
    except DataUnavailableError:
        pass


def test_api_endpoint():
    import pytest
    pytest.importorskip("fastapi"); pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from backend.api.main import create_app
    from test_providers_and_service import xaus_env

    settings, clock, fake, provider, service = xaus_env()
    assert TestClient(create_app(service=service, settings=settings)).get("/api/calendar").status_code == 503
    cal, _ = make([ev("CPI m/m", 20)])
    r = TestClient(create_app(service=service, settings=settings, calendar=cal)).get("/api/calendar")
    assert r.status_code == 200 and r.json()["warning"]["state"] == "NO_TRADE"
