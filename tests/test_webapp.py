from __future__ import annotations

from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_live_flights,
    get_telemetry,
)
from pyaerial.store.redis_live import RedisLiveStore
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

    config = make_config()
    app = create_app(config=config, db=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        spec = client.get("/api")
        assert spec.status_code == 200
        body = spec.json()
        assert body["websocket"] == "/ws/live"
        assert "fetchFlights" in body["actions"]
        assert client.get("/api/flights").status_code == 404
        assert client.get("/api/stats").status_code == 404


def test_websocket_hello_snapshot_and_subscribe():
    from fastapi.testclient import TestClient

    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    app = create_app(
        config=make_config(), db=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["protocol"] == "pyaerial.live"
            assert "fetchFlights" in hello["actions"]
            assert ws.receive_json()["type"] == "flights"
            assert ws.receive_json()["type"] == "alerts"
            assert ws.receive_json()["type"] == "stats"
            ws.send_json(
                {
                    "type": "request",
                    "id": "1",
                    "action": "subscribe",
                    "params": {"streams": ["flights"]},
                }
            )
            reply = ws.receive_json()
            assert reply["success"] is True
            assert reply["data"]["streams"] == ["flights"]
            ws.send_json(
                {
                    "type": "request",
                    "id": "2",
                    "action": "fetchStats",
                    "params": {},
                }
            )
            while True:
                stats = ws.receive_json()
                if stats.get("type") == "response" and stats.get("id") == "2":
                    break
            assert stats["success"] is True
            assert "live_flights" in stats["data"]
            assert stats["data"]["redis"] is True
            assert stats["data"]["mongo"] is False
            assert stats["data"]["engine_seen_at"] is None

        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "hello"


def test_websocket_rejects_disallowed_origin():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    config = make_config()
    config.web.origins = []
    app = create_app(config=config, db=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        try:
            with client.websocket_connect(
                "/ws/live", headers={"Origin": "https://evil.example"}
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected websocket to close")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008


def test_ready_uses_live_store_ping():
    from fastapi.testclient import TestClient

    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    assert store.ping() is True
    app = create_app(
        config=make_config(), db=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        body = ready.json()
        assert body["redis"] is True
        assert body["status"] == "ok"
