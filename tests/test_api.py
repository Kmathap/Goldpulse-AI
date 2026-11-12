"""API tests – run on your PC after `pip install -r requirements.txt`."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from backend.api.main import create_app  # noqa: E402
from test_providers_and_service import xaus_env  # noqa: E402


def client_for(**kw):
    settings, clock, fake, provider, service = xaus_env(**kw)
    return TestClient(create_app(service=service, settings=settings)), fake


def test_price_endpoint_live():
    client, _ = client_for()
    r = client.get("/api/price")
    assert r.status_code == 200 and r.json()["status"] == "LIVE"


def test_price_endpoint_unavailable_is_503():
    client, fake = client_for()
    fake.fail = {"chart"}
    r = client.get("/api/price")
    assert r.status_code == 503 and r.json()["status"] == "DATA_UNAVAILABLE"


def test_candles_and_technicals():
    client, _ = client_for()
    assert client.get("/api/candles/h1?count=50").json()["count"] == 50
    assert client.get("/api/technicals/M5").json()["timeframe"] == "M5"
    assert set(client.get("/api/technicals").json()["timeframes"]) == {"M5", "M15", "M30", "H1", "H4", "D1"}
    assert client.get("/api/technicals/W1").status_code == 400


def test_no_secrets_in_responses():
    client, _ = client_for()
    for path in ("/api/health", "/api/price"):
        assert "apikey" not in client.get(path).text.lower()
