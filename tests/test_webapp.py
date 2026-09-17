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
    assert get_flight_detail("x", "history", live_store=None, history=None, aircraft_db=None) is None
    assert get_telemetry("x", "history", 0.0, live_store=None, history=None) == []
    assert get_alerts("history", live_store=None, history=None) == []


def test_live_queries_tolerate_missing_store():
    assert get_live_flights(None, None) == []
    assert get_flight_detail("x", "live", live_store=None, history=None, aircraft_db=None) is None
    assert get_telemetry("x", "live", 0.0, live_store=None, history=None) == []
    assert get_alerts("live", live_store=None, history=None) == []


def test_create_app_without_frontend_serves_503():
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    assert app.title == "PyAerial Web Portal"


def test_health_and_api(monkeypatch):
    from fastapi.testclient import TestClient

    config = make_config()
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        spec = client.get("/api")
        assert spec.status_code == 200
        body = spec.json()
        assert body["websocket"] == "/ws/live"
        assert body["raw_websocket"] == "/ws/raw"
        assert "raw" in body["streams"]
        assert "fetchFlights" in body["actions"]
        assert client.get("/api/flights").status_code == 404
        assert client.get("/api/stats").status_code == 404


def test_websocket_hello_snapshot_and_subscribe():
    from fastapi.testclient import TestClient

    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["protocol"] == "pyaerial.live"
            assert "fetchFlights" in hello["actions"]
            assert "raw" in hello["streams"]
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
            assert stats["data"]["history"] is False
            assert stats["data"]["engine_seen_at"] is None

        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "hello"


def test_websocket_raw_endpoint_and_publish():
    from fastapi.testclient import TestClient

    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/raw") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            antenna = ws.receive_json()
            assert antenna["type"] == "antenna"
            assert "home" in antenna["antenna"]
            assert antenna["antenna"]["receivers"][0]["name"] == "main"
            store.publish_raw(
                [
                    {
                        "hex": "8d406b902015a678d4d220aa4bda",
                        "timestamp": 1.0,
                        "receiver": "main",
                        "rssi": -18.5,
                        "df": 17,
                        "icao": "406b90",
                    }
                ]
            )
            raw = ws.receive_json()
            assert raw["type"] == "raw"
            assert raw["messages"][0]["hex"] == "8d406b902015a678d4d220aa4bda"
            assert raw["messages"][0]["rssi"] == -18.5


def test_websocket_streams_query_param_raw_only():
    from fastapi.testclient import TestClient

    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live?streams=raw") as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "antenna"


def test_websocket_subscribe_raw_sends_antenna():
    from fastapi.testclient import TestClient

    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "flights"
            assert ws.receive_json()["type"] == "alerts"
            assert ws.receive_json()["type"] == "stats"
            ws.send_json(
                {
                    "type": "request",
                    "id": "raw",
                    "action": "subscribe",
                    "params": {"streams": ["raw"]},
                }
            )
            reply = ws.receive_json()
            assert reply["success"] is True
            assert reply["data"]["streams"] == ["raw"]
            antenna = ws.receive_json()
            assert antenna["type"] == "antenna"


def test_websocket_rejects_disallowed_origin():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    config = make_config()
    config.web.origins = []
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
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
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        body = ready.json()
        assert body["redis"] is True
        assert body["status"] == "ok"
