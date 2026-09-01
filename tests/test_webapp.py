from __future__ import annotations

from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_live_flights,
    get_telemetry,
)
from pyaerial.webapp import create_app
from helpers import make_config


def test_history_queries_tolerate_missing_db():
    assert get_history_flights(None, None) == []
    assert get_flight_detail("x", "history", live_store=None, db=None, aircraft_db=None) is None
    assert get_telemetry("x", "history", 0.0, live_store=None, db=None) == []
    assert get_alerts("history", live_store=None, db=None) == []


def test_live_queries_tolerate_missing_store():
    assert get_live_flights(None, None) == []
    assert get_flight_detail("x", "live", live_store=None, db=None, aircraft_db=None) is None
    assert get_telemetry("x", "live", 0.0, live_store=None, db=None) == []
    assert get_alerts("live", live_store=None, db=None) == []


def test_create_app_without_frontend_serves_503():
    app = create_app(config=make_config(), db=None, live_store=None, aircraft_db=None)
    assert app.title == "PyAerial Web Portal"


def test_health_and_api(monkeypatch):
    from fastapi.testclient import TestClient
    from pyaerial.config.schema import WebConfig

    config = make_config()
    app = create_app(config=config, db=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        flights = client.get("/api/flights")
        assert flights.status_code == 200
        assert flights.json() == []

    config.web = WebConfig(token="s3cret")
    locked = create_app(config=config, db=None, live_store=None, aircraft_db=None)
    with TestClient(locked) as client:
        assert client.get("/api/stats").status_code == 401
        assert client.get("/api/stats", params={"token": "s3cret"}).status_code == 200
