import pytest

from backend.data.base import DataUnavailableError
from backend.data.related_markets import CACHE_SECONDS, RelatedMarkets
from fakes import Clock


def q(close, prev, pct=None):
    d = {"close": str(close), "previous_close": str(prev), "is_market_open": True}
    if pct is not None:
        d["percent_change"] = str(pct)
    return d


class Stub:
    def __init__(self, body):
        self.body, self.calls, self.params = body, 0, None

    def __call__(self, url, params=None):
        self.calls += 1
        self.params = params
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def make(body, clock=None):
    stub = Stub(body)
    return RelatedMarkets("k", http_get=stub, clock=clock or Clock()), stub


BEAR = {
    "XAU/USD": q(3577.7, 3685.5, -2.92), "XAG/USD": q(29.1, 30.0, -3.0), "WTI/USD": q(70, 71, -1.4),
    "EUR/USD": q(1.05, 1.055, -0.47), "GBP/USD": q(1.25, 1.256, -0.48), "USD/JPY": q(156, 155, 0.65),
}


def test_gold_down_confirmed_by_stronger_dollar_and_weaker_silver():
    rm, stub = make(BEAR)
    p = rm.payload()
    assert p["verdict"]["state"] == "CONFIRMS"
    assert p["verdict"]["direction"] == "DOWN"
    assert p["dollar_change_pct"] > 0
    assert "stronger" in p["verdict"]["text"]
    assert "XAU/USD" in stub.params["symbol"] and stub.params["apikey"] == "k"
    usdjpy = next(m for m in p["markets"] if m["symbol"] == "USD/JPY")
    assert usdjpy["gold_effect"] == -1          # dollar up vs yen -> bad for gold


def test_conflict_and_mixed():
    body = dict(BEAR, **{"XAG/USD": q(31, 30, 3.3), "EUR/USD": q(1.06, 1.05, 0.9), "GBP/USD": q(1.27, 1.26, 0.8),
                         "USD/JPY": q(154, 155, -0.6)})
    assert make(body)[0].payload()["verdict"]["state"] == "CONFLICTS"
    body = dict(BEAR, **{"XAG/USD": q(31, 30, 3.3)})
    assert make(body)[0].payload()["verdict"]["state"] == "MIXED"


def test_flat_gold_and_missing_symbol():
    body = dict(BEAR, **{"XAU/USD": q(3600, 3601, -0.03), "WTI/USD": {"code": 404, "message": "not found", "status": "error"}})
    p = make(body)[0].payload()
    assert p["verdict"]["state"] == "FLAT"
    wti = next(m for m in p["markets"] if m["symbol"] == "WTI/USD")
    assert wti["available"] is False and "plan" in wti["note"]


def test_cached_and_needs_key():
    clock = Clock()
    rm, stub = make(BEAR, clock)
    rm.payload(); rm.payload()
    assert stub.calls == 1
    clock.t += CACHE_SECONDS + 1
    rm.payload()
    assert stub.calls == 2
    with pytest.raises(DataUnavailableError):
        RelatedMarkets(None).payload()


def test_whole_call_error():
    rm, _ = make({"code": 401, "message": "bad key", "status": "error"})
    with pytest.raises(DataUnavailableError):
        rm.payload()
