from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helpers import make_config
from pyaerial.api import static
from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_live_flights,
    get_telemetry,
)
from pyaerial.store.live import LiveWriter
from pyaerial.webapp import create_app


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
        assert body["beast_websocket"] == "/ws/beast"
        assert body["raw_websocket"] == "/ws/raw"
        assert "raw" not in body["streams"]
        assert "flights" in body["streams"]
        assert "fetchFlights" in body["actions"]
        assert "Beast" in body["beast"]["format"]
        assert "/ws/beast" in body["connect"]
        assert "/ws/raw" in body["connect"]
        assert "?streams=" in body["connect"]
        assert "auth" not in body
        assert client.get("/api/flights").status_code == 404
        assert client.get("/api/stats").status_code == 404


def test_websocket_hello_snapshot_and_subscribe(fake):
    from fastapi.testclient import TestClient

    store = LiveWriter("redis://localhost:6379/0")
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["protocol"] == "pyaerial.live"
            assert "fetchFlights" in hello["actions"]
            assert "raw" not in hello["streams"]
            assert "beast" not in hello["streams"]
            assert "flights" in hello["streams"]
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


def test_subscribe_sends_a_snapshot_of_added_streams(fake):
    """Subscribing to alerts delivers the current alerts, not just later changes."""
    import time

    from fastapi.testclient import TestClient

    store = LiveWriter("redis://localhost:6379/0")
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        # Pretend the empty lists were already pushed, so no tick re-sends them.
        app.state.broadcaster._last_flights_sig = ()
        app.state.broadcaster._last_alerts_sig = ()
        with client.websocket_connect("/ws/live?streams=stats") as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "stats"
            ws.send_json(
                {
                    "type": "request",
                    "id": "s",
                    "action": "subscribe",
                    "params": {"streams": ["stats", "alerts"]},
                }
            )
            time.sleep(0.3)
            ws.send_json({"type": "request", "id": "z", "action": "fetchZones"})
            seen = []
            while True:
                message = ws.receive_json()
                seen.append(message.get("type"))
                if message.get("type") == "response" and message.get("id") == "z":
                    break
            assert "alerts" in seen
            assert "flights" not in seen


def test_websocket_beast_endpoint_and_publish(fake):
    from fastapi.testclient import TestClient

    from pyaerial.receivers.frames import BeastParser

    store = LiveWriter("redis://localhost:6379/0")
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    hex_msg = "8d406b902015a678d4d220aa4bda"
    with TestClient(app) as client:
        with client.websocket_connect("/ws/beast") as ws:
            store.publish_raw(
                [
                    {
                        "hex": hex_msg,
                        "timestamp": 1.0,
                        "receiver": "main",
                        "rssi": -18.5,
                        "clock": 12,
                    }
                ]
            )
            raw = ws.receive_bytes()
            frames = BeastParser().feed(raw)
            assert len(frames) == 1
            assert frames[0][0] == hex_msg
            assert frames[0][2] == 12_000_000


def test_websocket_raw_endpoint_and_publish(fake):
    from fastapi.testclient import TestClient

    store = LiveWriter("redis://localhost:6379/0")
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    hex_msg = "8d406b902015a678d4d220aa4bda"
    frame = {
        "hex": hex_msg,
        "timestamp": 1.5,
        "receiver": "strong",
        "receivers": ["strong", "weak"],
        "rssi": -8.0,
        "clock": 12,
        "df": 17,
        "icao": "406b90",
    }
    with TestClient(app) as client:
        with client.websocket_connect("/ws/raw") as raw_ws:
            with client.websocket_connect("/ws/beast") as beast_ws:
                store.publish_raw([frame])
                message = raw_ws.receive_json()
                assert message == {"type": "raw", "frames": [frame]}
                assert beast_ws.receive_bytes()


def test_websocket_live_ignores_beast_stream(fake):
    from fastapi.testclient import TestClient

    store = LiveWriter("redis://localhost:6379/0")
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live?streams=raw") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert "raw" not in hello["streams"]
            assert "beast" not in hello["streams"]
            assert ws.receive_json()["type"] == "flights"
            assert ws.receive_json()["type"] == "alerts"
            assert ws.receive_json()["type"] == "stats"
            store.publish_raw(
                [{"hex": "8d406b902015a678d4d220aa4bda", "timestamp": 1.0, "clock": 1}]
            )
            ws.send_json(
                {
                    "type": "request",
                    "id": "raw",
                    "action": "subscribe",
                    "params": {"streams": ["raw"]},
                }
            )
            while True:
                reply = ws.receive_json()
                assert reply.get("type") != "raw"
                assert reply.get("type") != "antenna"
                if reply.get("type") == "response" and reply.get("id") == "raw":
                    break
            assert reply["success"] is False


def test_beast_tcp_streams_frames(fake):
    import asyncio

    from fastapi.testclient import TestClient

    from pyaerial.receivers.frames import BeastParser

    config = make_config()
    config.web.beast_host = "127.0.0.1"
    config.web.beast_port = 0
    store = LiveWriter("redis://localhost:6379/0")
    app = create_app(
        config=config, history=None, live_store=store, aircraft_db=None
    )
    hex_msg = "8d406b902015a678d4d220aa4bda"
    with TestClient(app) as client:
        spec = client.get("/api").json()
        port = spec["beast"]["tcp_port"]
        assert port and port > 0

        async def _recv() -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)
            store.publish_raw(
                [{"hex": hex_msg, "timestamp": 1.0, "clock": 4, "rssi": -12.0}]
            )
            data = await asyncio.wait_for(reader.read(4096), timeout=3)
            writer.close()
            await writer.wait_closed()
            return data

        raw = asyncio.run(_recv())
        frames = BeastParser().feed(raw)
        assert frames[0][0] == hex_msg
        assert frames[0][2] == 12_000_000


def test_websocket_accepts_same_origin_with_explicit_allowlist():
    """The portal's own origin (Origin == Host) works with any web.origins."""
    from fastapi.testclient import TestClient

    config = make_config()
    config.web.origins = ["https://portal.example"]
    config.web.allowed_hosts = ["testserver"]
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/live",
            headers={"Origin": "http://testserver", "Host": "testserver"},
        ) as ws:
            assert ws.receive_json()["type"] == "hello"


def test_websocket_rejects_untrusted_host_when_allowed_hosts_set():
    """web.allowed_hosts blocks DNS-rebinding (attacker origin == attacker Host)."""
    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketDenialResponse

    config = make_config()
    config.web.allowed_hosts = ["portal.example"]
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app, base_url="http://rebind.attacker.example") as client:
        with pytest.raises(WebSocketDenialResponse):
            with client.websocket_connect(
                "/ws/live",
                headers={"Origin": "http://rebind.attacker.example"},
            ) as ws:
                ws.receive_json()
        assert client.get("/health").status_code == 400


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


def test_ready_uses_live_store_ping(fake):
    from fastapi.testclient import TestClient

    store = LiveWriter("redis://localhost:6379/0")
    assert store.ping() is True
    app = create_app(
        config=make_config(), history=None, live_store=store, aircraft_db=None
    )
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        body = ready.json()
        assert body["redis"] is True
        assert body["engine"] is False
        assert body["status"] == "degraded"
        store.touch_engine()
        ready = client.get("/ready")
        assert ready.status_code == 200
        body = ready.json()
        assert body["engine"] is True
        assert body["status"] == "ok"


def test_allowed_hosts_keeps_loopback_reachable():
    """Setting web.allowed_hosts must not break the 127.0.0.1 container healthcheck."""
    from fastapi.testclient import TestClient

    config = make_config()
    config.web.allowed_hosts = ["portal.example"]
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    for base in ("http://127.0.0.1:10090", "http://localhost:5173", "http://portal.example"):
        with TestClient(app, base_url=base) as client:
            assert client.get("/health").status_code == 200, base


# --- SPA static serving ------------------------------------------------------
# Missing files 404, the shell is never cached.


def test_spa_fallback_and_missing_files(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>shell</html>")
    (tmp_path / "robots.txt").write_text("User-agent: *")
    monkeypatch.setattr(static, "STATIC_DIR", tmp_path)
    app = create_app(config=make_config())
    with TestClient(app) as client:
        for path in ("/", "/index.html", "/some/client/route"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.text == "<html>shell</html>"
            assert response.headers["cache-control"] == "no-store", path
        assert client.get("/robots.txt").text == "User-agent: *"
        # A missing file must not come back as the HTML shell (a stale bundle
        # would then fail with a MIME-type error instead of a clean 404).
        for path in ("/missing.js", "/nested/missing.css"):
            assert client.get(path).status_code == 404, path
        assert client.get("/..%2f..%2fpyproject.toml").status_code == 404
