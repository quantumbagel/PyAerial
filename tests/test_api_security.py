"""Regression tests for the web API security and robustness fixes."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers import make_config
from pyaerial.api.app import create_app
from pyaerial.api.security import (
    BucketRegistry,
    ConnectionLimiter,
    TokenBucket,
    content_security_policy,
    origin_allowed,
)
from pyaerial.store.archive import HistoryUnavailable
from pyaerial.store.live import LiveWriter
from pyaerial.webapp import uvicorn_options


def _memory_store() -> LiveWriter:
    """A live store on the ``fake`` Redis (the test must request it)."""
    return LiveWriter("redis://localhost:6379/0")


def _drain_snapshot(ws) -> None:
    assert ws.receive_json()["type"] == "hello"
    seen = set()
    while not {"flights", "alerts", "stats"} <= seen:
        seen.add(ws.receive_json()["type"])


def _response(ws, req_id):
    while True:
        message = ws.receive_json()
        if message.get("type") == "response" and message.get("id") == req_id:
            return message


def _request(ws, req_id, action, **params):
    ws.send_json({"type": "request", "id": req_id, "action": action, "params": params})
    return _response(ws, req_id)


# -- origin policy -----------------------------------------------------------


def test_origin_matching_is_exact_scheme_host_port():
    allowed = ["https://portal.example"]
    assert origin_allowed("https://portal.example", allowed)
    assert origin_allowed("https://portal.example:443", allowed)
    assert not origin_allowed("http://portal.example", allowed)
    assert not origin_allowed("https://portal.example:8443", allowed)
    assert not origin_allowed("https://evil.portal.example", allowed)
    # Bare hostnames no longer match any scheme/port.
    assert not origin_allowed("https://portal.example", ["portal.example"])
    assert not origin_allowed("null", allowed)
    assert origin_allowed(None, allowed)
    assert origin_allowed("https://anything.example", ["*"])


def test_localhost_origins_are_opt_in():
    assert not origin_allowed("http://localhost:5173", [])
    assert not origin_allowed("http://127.0.0.1:5173", [])
    assert origin_allowed("http://localhost:5173", [], allow_localhost=True)
    assert origin_allowed("http://[::1]:5173", [], allow_localhost=True)
    assert not origin_allowed(
        "http://localhost.evil.example", [], allow_localhost=True
    )


def test_same_origin_uses_host_header():
    hosts = ["pi.local", "tracker.example"]
    assert origin_allowed(
        "http://pi.local:10090", [], host_header="pi.local:10090", allowed_hosts=hosts
    )
    assert origin_allowed(
        "https://tracker.example", [], host_header="tracker.example", allowed_hosts=hosts
    )
    assert origin_allowed("http://192.168.1.20:10090", [], host_header="192.168.1.20:10090")
    assert origin_allowed("http://[::1]:10090", [], host_header="[::1]:10090")
    assert origin_allowed("http://localhost:10090", [], host_header="localhost:10090")
    assert not origin_allowed("http://pi.local:8080", [], host_header="pi.local:10090")
    assert not origin_allowed("https://evil.example", [], host_header="pi.local")


def test_same_origin_hostname_needs_allowed_hosts():
    # DNS rebinding: attacker.example re-resolves to the portal, so Origin == Host.
    assert not origin_allowed(
        "http://attacker.example:10090", [], host_header="attacker.example:10090"
    )
    assert origin_allowed(
        "http://pi.lan:10090", [], host_header="pi.lan:10090", allowed_hosts=["*.lan"]
    )


def test_default_config_rejects_foreign_and_localhost_browser_origins():
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        for origin in ("https://evil.example", "http://localhost:5173"):
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect(
                    "/ws/live", headers={"Origin": origin}
                ) as ws:
                    ws.receive_json()
            assert excinfo.value.code == 1008
        # Same-origin by IP address is trusted without configuration...
        with client.websocket_connect(
            "/ws/live",
            headers={"Origin": "http://192.168.1.20:10090", "Host": "192.168.1.20:10090"},
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
        # ...but an unlisted hostname is not (DNS rebinding looks exactly like this).
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/ws/live", headers={"Origin": "http://testserver"}
            ) as ws:
                ws.receive_json()
        assert excinfo.value.code == 1008


def test_allow_localhost_origins_flag():
    config = make_config()
    config.web.allow_localhost_origins = True
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/live", headers={"Origin": "http://localhost:5173"}
        ) as ws:
            assert ws.receive_json()["type"] == "hello"


# -- docs + headers -----------------------------------------------------------


def test_openapi_docs_disabled_by_default():
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        # Falls through to the SPA handler (index.html or 503), never the schema.
        assert '"openapi"' not in client.get("/openapi.json").text
        assert "swagger" not in client.get("/docs").text.lower()
        assert "redoc" not in client.get("/redoc").text.lower()


def test_openapi_docs_can_be_enabled():
    config = make_config()
    config.web.enable_docs = True
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        assert "swagger" in client.get("/docs").text.lower()


def test_security_headers_on_responses():
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        for path in ("/health", "/", "/some/spa/route"):
            response = client.get(path)
            csp = response.headers["content-security-policy"]
            assert "frame-ancestors 'none'" in csp
            assert "default-src 'self'" in csp
            assert "ws://testserver" in csp
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"


def test_csp_allows_exactly_what_the_portal_loads():
    csp = content_security_policy("pi.local:10090")
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp
    assert "https://tiles.openfreemap.org" in csp
    assert "worker-src 'self' blob:" in csp
    assert "script-src 'self';" in csp
    assert "wss://pi.local:10090" in csp
    # A hostile Host header cannot inject extra directives.
    assert "evil" not in content_security_policy("x; script-src evil")


# -- connection caps and request limits -----------------------------------


def test_connection_limiter_counts_total_and_per_ip():
    limiter = ConnectionLimiter(max_total=3, max_per_ip=2)
    assert limiter.acquire("a") is None
    assert limiter.acquire("a") is None
    assert limiter.acquire("a") is not None
    assert limiter.acquire("b") is None
    assert limiter.acquire("c") is not None
    limiter.release("a")
    assert limiter.acquire("c") is None
    limiter.release("unknown")
    assert limiter.total == 3


def test_token_bucket_refills():
    now = [0.0]
    bucket = TokenBucket(rate=1.0, burst=2, clock=lambda: now[0])
    assert bucket.take() and bucket.take()
    assert not bucket.take()
    now[0] = 1.0
    assert bucket.take()
    assert not bucket.take()


def test_token_bucket_reserve_throttles_then_refuses():
    now = [0.0]
    bucket = TokenBucket(rate=10.0, burst=1, clock=lambda: now[0])
    assert bucket.reserve(1.0) == 0.0
    assert bucket.reserve(1.0) == pytest.approx(0.1)
    assert bucket.reserve(1.0) == pytest.approx(0.2)
    slow = TokenBucket(rate=0.5, burst=1, clock=lambda: now[0])
    assert slow.reserve(1.0) == 0.0
    assert slow.reserve(1.0) is None


def test_ws_requests_over_rate_are_delayed_not_dropped():
    config = make_config()
    config.web.ws_rate_burst = 1
    config.web.ws_rate_limit = 20.0
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            started = time.monotonic()
            for index in range(4):
                assert _request(ws, str(index), "fetchZones")["success"] is True
            assert time.monotonic() - started >= 0.12


def test_ws_raw_rejects_disallowed_origin():
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/ws/raw", headers={"Origin": "https://evil.example"}
            ) as ws:
                ws.receive_text()
        assert excinfo.value.code == 1008


def test_per_ip_ws_cap_rejects_extra_connections():
    config = make_config()
    config.web.ws_max_connections_per_ip = 1
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as first:
            assert first.receive_json()["type"] == "hello"
            with pytest.raises(WebSocketDisconnect) as excinfo:
                with client.websocket_connect("/ws/beast") as second:
                    second.receive_bytes()
            assert excinfo.value.code == 1013
        # The slot is released when the first socket closes.
        with client.websocket_connect("/ws/live") as again:
            assert again.receive_json()["type"] == "hello"
        assert app.state.ws_limiter.total <= 1


def test_ws_rate_limit_returns_rate_limited_code():
    config = make_config()
    config.web.ws_rate_burst = 2
    config.web.ws_rate_limit = 0.001
    app = create_app(config=config, history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            assert _request(ws, "1", "fetchZones")["success"] is True
            assert _request(ws, "2", "fetchZones")["success"] is True
            limited = _request(ws, "3", "fetchZones")
            assert limited["success"] is False
            assert limited["code"] == "rate_limited"


def test_bad_json_is_not_logged_as_error(caplog):
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with caplog.at_level(logging.DEBUG, logger="pyaerial.webapp"):
            with client.websocket_connect("/ws/live") as ws:
                ws.send_text("{not json")
                while True:
                    message = ws.receive_json()
                    if message.get("type") == "response":
                        break
                assert message["code"] == "bad_request"
                ws.send_bytes(b"\x00\x01")
                while True:
                    message = ws.receive_json()
                    if message.get("type") == "response":
                        break
                assert message["code"] == "bad_request"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# -- request errors and caps -------------------------------------------------


class _FakeHistory:
    def __init__(self, *, alive: bool = True, points: int = 0):
        self.alive = alive
        self.points = points
        self.calls: list[dict] = []

    def ping(self) -> bool:
        return self.alive

    def list_flights(self, **kwargs):
        self.calls.append(kwargs)
        return []

    def count_flights(self) -> int:
        return 0

    def count_alerts(self) -> int:
        return 0

    def get_flight(self, flight_id):
        raise HistoryUnavailable("disk gone")

    def _points(self, n):
        return [{"timestamp": float(i), "altitude": 1.0} for i in range(n)]

    def get_telemetry(self, flight_id, *, since=0.0, limit=None, newest=False):
        self.calls.append({"since": since, "limit": limit, "newest": newest})
        points = [p for p in self._points(self.points) if p["timestamp"] > since]
        if limit:
            points = points[-limit:] if newest else points[:limit]
        return points


def test_ws_error_codes_distinguish_failures():
    history = _FakeHistory()
    app = create_app(config=make_config(), history=history, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            _drain_snapshot(ws)
            missing = _request(ws, "a", "fetchFlight", view="history")
            assert missing["code"] == "bad_request"
            assert "flightId" in missing["error"]
            gone = _request(ws, "b", "fetchFlight", view="history", flightId="x")
            assert gone["code"] == "history_unavailable"
            unknown = _request(ws, "c", "launchMissiles")
            assert unknown["code"] == "bad_request"
            nan = _request(ws, "d", "fetchAlerts", view="history", since="nan")
            assert nan["code"] == "bad_request"
            history.alive = False
            down = _request(ws, "e", "fetchFlights", view="history")
            assert down["code"] == "history_unavailable"


def test_fetch_flights_skip_is_capped():
    history = _FakeHistory()
    app = create_app(config=make_config(), history=history, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            _drain_snapshot(ws)
            reply = _request(ws, "s", "fetchFlights", view="history", skip=10**9)
            assert reply["success"] is True
    assert history.calls[-1]["skip"] == 10_000


def test_history_telemetry_is_capped():
    from pyaerial.api.queries import MAX_HISTORY_TELEMETRY_POINTS, get_telemetry

    total = MAX_HISTORY_TELEMETRY_POINTS + 500
    history = _FakeHistory(points=total)
    points = get_telemetry("f", "history", 0.0, live_store=None, history=history)
    assert len(points) == MAX_HISTORY_TELEMETRY_POINTS
    # Without a cursor the cap keeps the newest points: the path ends where the flight did.
    assert points[-1]["timestamp"] == float(total - 1)
    assert history.calls[-1] == {
        "since": 0.0,
        # One extra row detects truncation (r5: ``points.truncated``).
        "limit": MAX_HISTORY_TELEMETRY_POINTS + 1,
        "newest": True,
    }
    assert points.truncated is True

    limited = _FakeHistory(points=50)
    points = get_telemetry("f", "history", 0.0, live_store=None, history=limited, limit=10)
    assert [p["timestamp"] for p in points] == [float(t) for t in range(40, 50)]

    # With a cursor it pages forward from it.
    points = get_telemetry("f", "history", 5.0, live_store=None, history=limited, limit=3)
    assert [p["timestamp"] for p in points] == [6.0, 7.0, 8.0]


def test_ws_request_ids_are_bounded():
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            _drain_snapshot(ws)
            ws.send_json(
                {"type": "request", "id": "x" * 5000, "action": "fetchZones", "params": {}}
            )
            reply = _response(ws, None)
            assert reply["success"] is True


# -- stats / engine_live -----------------------------------------------------


def test_stats_include_server_side_engine_live(fake):
    from pyaerial.api.queries import engine_is_live, get_stats

    store = _memory_store()
    assert get_stats(store, None)["engine_live"] is False
    store.touch_engine()
    assert get_stats(store, None)["engine_live"] is True
    assert engine_is_live(None) is False

    class _SeenOnly:
        def __init__(self, seen):
            self.seen = seen

        def engine_seen_at(self):
            return self.seen

    assert engine_is_live(_SeenOnly(time.time())) is True
    assert engine_is_live(_SeenOnly(time.time() - 3600)) is False
    assert engine_is_live(_SeenOnly(None)) is False


# -- lifecycle -----------------------------------------------------------------


def test_lifespan_stops_broadcaster_when_beast_start_fails(monkeypatch):
    app = create_app(config=make_config(), history=None, live_store=None, aircraft_db=None)
    broadcaster = app.state.broadcaster

    async def _boom():
        raise RuntimeError("beast failed")

    monkeypatch.setattr(app.state.beast_hub, "start", _boom)
    with pytest.raises(RuntimeError, match="beast failed"):
        with TestClient(app):
            pass
    assert broadcaster._task is None


def test_snapshot_failure_does_not_leave_zombie_client(fake, monkeypatch):
    store = _memory_store()
    app = create_app(config=make_config(), history=None, live_store=store, aircraft_db=None)
    broadcaster = app.state.broadcaster

    def _broken_stats(*_args, **_kwargs):
        raise RuntimeError("stats exploded")

    monkeypatch.setattr("pyaerial.api.broadcaster.get_stats", _broken_stats)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            # The hello may or may not be flushed before the server closes (it
            # is not on 3.14's scheduler), so the first receive may raise too.
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_json()
        assert broadcaster.client_count == 0
        assert app.state.ws_limiter.total == 0


def test_beast_hub_stop_returns_with_idle_tcp_client():
    from pyaerial.api.beast import BeastHub

    async def _scenario() -> float:
        hub = BeastHub(None, host="127.0.0.1", port=0)
        await hub.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
        for _ in range(50):
            if hub.tcp_client_count:
                break
            await asyncio.sleep(0.01)
        assert hub.tcp_client_count == 1
        started = time.monotonic()
        await asyncio.wait_for(hub.stop(), timeout=5)
        elapsed = time.monotonic() - started
        assert await asyncio.wait_for(reader.read(), timeout=2) == b""
        writer.close()
        return elapsed

    assert asyncio.run(_scenario()) < 1.5


def test_beast_hub_enforces_max_clients():
    from pyaerial.api.beast import BeastHub

    async def _scenario() -> None:
        hub = BeastHub(None, host="127.0.0.1", port=0, max_clients=1)
        await hub.start()
        try:
            _r1, w1 = await asyncio.open_connection("127.0.0.1", hub.port)
            for _ in range(50):
                if hub.tcp_client_count:
                    break
                await asyncio.sleep(0.01)
            r2, w2 = await asyncio.open_connection("127.0.0.1", hub.port)
            assert await asyncio.wait_for(r2.read(), timeout=2) == b""
            assert hub.tcp_client_count == 1
            w1.close()
            w2.close()
        finally:
            await hub.stop()

    asyncio.run(_scenario())


def test_beast_tcp_write_error_detaches_client():
    from pyaerial.api.beast import BeastHub

    class _BrokenWriter:
        def __init__(self):
            self.closed = False

        def write(self, _data):
            raise ConnectionResetError("peer gone")

        async def drain(self):
            return None

        def close(self):
            self.closed = True

    async def _scenario() -> None:
        hub = BeastHub(None, port=None)
        queue = hub._attach()
        writer = _BrokenWriter()
        queue.put_nowait(b"\x1a\x32")
        await asyncio.wait_for(hub._tcp_writer(writer, queue), timeout=2)
        assert queue not in hub._queues
        assert writer.closed

    asyncio.run(_scenario())


# --- Per-IP buckets survive reconnects ---------------------------------------


class _ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- per-IP buckets ------------------------------------------------------------


def test_bucket_outlives_the_connection_until_refilled():
    clock = _ManualClock()
    registry = BucketRegistry(rate=1.0, burst=3, clock=clock)
    bucket = registry.acquire("1.2.3.4")
    for _ in range(3):
        assert bucket.reserve(0.0) == 0.0
    registry.release("1.2.3.4")
    # Reconnecting right away gets the same, empty bucket (no fresh burst).
    assert registry.acquire("1.2.3.4") is bucket
    assert bucket.reserve(0.0) is None
    registry.release("1.2.3.4")
    clock.now += 10  # refilled, and idle: evicted on the next sweep
    registry.sweep()
    assert "1.2.3.4" not in registry


def test_buckets_in_use_or_in_debt_are_kept():
    clock = _ManualClock()
    registry = BucketRegistry(rate=1.0, burst=2, clock=clock)
    busy = registry.acquire("busy")
    registry.acquire("debtor").reserve(0.0)
    registry.acquire("debtor").reserve(0.0)
    registry.release("debtor")
    registry.release("debtor")
    registry.sweep()
    assert "busy" in registry and "debtor" in registry
    assert busy is registry.acquire("busy")


def test_registry_is_bounded():
    clock = _ManualClock()
    registry = BucketRegistry(rate=0.001, burst=2, max_entries=8, clock=clock)
    for i in range(50):
        registry.acquire(f"10.0.0.{i}").reserve(0.0)  # each in debt
        registry.release(f"10.0.0.{i}")
        clock.now += 0.01
    assert len(registry) <= 8
    assert "10.0.0.49" in registry  # the least recently used went first


def test_reconnecting_does_not_reset_the_per_ip_budget():
    config = make_config()
    config.web.ws_rate_limit = 1000
    config.web.ws_rate_burst = 1000
    config.web.ws_rate_limit_per_ip = 0.01
    config.web.ws_rate_burst_per_ip = 2
    app = create_app(config=config)

    def _codes(ws, count: int) -> list:
        ws.receive_json()  # hello
        codes = []
        for i in range(count):
            ws.send_json({"type": "request", "id": i, "action": "fetchZones"})
            while (reply := ws.receive_json()).get("type") != "response":
                pass
            codes.append(reply.get("code"))
        return codes

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live?streams=stats") as ws:
            assert _codes(ws, 2) == [None, None]
        with client.websocket_connect("/ws/live?streams=stats") as ws:
            assert _codes(ws, 1) == ["rate_limited"]


# --- Slow-loris --------------------------------------------------------------
# Idle and half-sent requests are closed; WebSockets are not.


@pytest.fixture
def server():
    config = make_config()
    app = create_app(config=config)
    options = uvicorn_options(config, header_timeout=0.5)
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, **options))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started and time.monotonic() < deadline:
        time.sleep(0.02)
    port = uv.servers[0].sockets[0].getsockname()[1]
    yield port
    uv.should_exit = True
    thread.join(5)


def _closed_within(sock: socket.socket, seconds: float) -> bool:
    sock.settimeout(seconds)
    try:
        while True:
            if sock.recv(4096) == b"":
                return True
    except TimeoutError:
        return False
    except OSError:
        return True


def test_options_bound_connections():
    options = uvicorn_options(make_config())
    assert options["limit_concurrency"] >= 2 * make_config().web.ws_max_connections
    assert options["timeout_keep_alive"] <= 5
    assert options["log_config"] is None  # our formatter, not uvicorn's


def test_idle_and_partial_requests_are_closed(server):
    idle = socket.create_connection(("127.0.0.1", server))
    partial = socket.create_connection(("127.0.0.1", server))
    partial.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")
    assert _closed_within(idle, 3)
    assert _closed_within(partial, 3)


def test_websocket_outlives_the_header_timeout(server):
    import base64
    import os

    sock = socket.create_connection(("127.0.0.1", server))
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET /ws/live?streams=stats HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    assert b"101" in sock.recv(4096).split(b"\r\n", 1)[0]
    time.sleep(1.5)  # three header timeouts
    sock.settimeout(0.5)
    try:
        data = sock.recv(4096)
    except TimeoutError:
        data = b"still open"
    assert data != b""
    sock.close()


def test_a_normal_request_still_works(server):
    sock = socket.create_connection(("127.0.0.1", server))
    sock.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    assert sock.recv(4096).startswith(b"HTTP/1.1 200")
    sock.close()
