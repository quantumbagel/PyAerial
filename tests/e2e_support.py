"""Shared plumbing for the end-to-end tests (tests/test_e2e_*.py).

Everything here is a stand-in for something *outside* PyAerial: a dump1090
Beast feeder, a webhook endpoint / receiver.json server, the public aircraft
APIs, Redis, and a browser/OpenSky client of the portal. PyAerial itself runs
unmodified: the real Engine (receivers, tracker, integrity, alerts, stores)
and the real FastAPI portal served by uvicorn on a loopback port.
"""

from __future__ import annotations

import contextlib
import json
import math
import queue
import socket
import socketserver
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import redis
import requests
from ruamel.yaml import YAML

from fake_redis import FakePipeline, FakeRedis
from integrity_world import with_crc

LOOPBACK = "127.0.0.1"
# Environment overrides the config loader honours; cleared so a developer's
# shell cannot change what the tests run against.
PYAERIAL_ENV = (
    "PYAERIAL_HISTORY",
    "PYAERIAL_REDIS",
    "PYAERIAL_LOG_LEVEL",
    "PYAERIAL_LOG_FILE",
    "PYAERIAL_HZ",
    "PYAERIAL_WEB_ORIGINS",
    "PYAERIAL_WEB_ALLOWED_HOSTS",
    "PYAERIAL_BEAST_PORT",
    "PYAERIAL_BEAST_HOST",
)

# Real clocks captured at import, before any test patches time.time.
_real_time = time.time
_real_monotonic = time.monotonic


def wait_for(
    predicate: Callable[[], Any],
    timeout: float,
    *,
    interval: float = 0.05,
    message: str = "condition",
) -> Any:
    """Poll *predicate* until it returns something truthy; fail after *timeout*."""
    deadline = _real_monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if _real_monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout:.1f}s waiting for {message}"
            )
        time.sleep(interval)


# --- ADS-B frame encoding (independent of the code under test) ----------------

_CALLSIGN_CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"
_CPR_BITS = 1 << 17
FT_TO_M = 0.3048
KT_TO_KMH = 1.852


def cpr_nl(lat: float) -> int:
    """Number of longitude zones at *lat* (DO-260B, NZ = 15)."""
    lat = abs(lat)
    if lat == 0:
        return 59
    if lat == 87:
        return 2
    if lat > 87:
        return 1
    a = 1 - math.cos(math.pi / 30)
    b = math.cos(math.radians(lat)) ** 2
    return math.floor(2 * math.pi / math.acos(1 - a / b))


def cpr_encode(lat: float, lon: float, odd: int) -> tuple[int, int]:
    """Airborne CPR (YZ, XZ) for *lat*/*lon* in the even (0) or odd (1) format."""
    dlat = 360.0 / (60 - odd)
    yz = math.floor(_CPR_BITS * ((lat % dlat) / dlat) + 0.5)
    rlat = dlat * (yz / _CPR_BITS + math.floor(lat / dlat))
    dlon = 360.0 / max(cpr_nl(rlat) - odd, 1)
    xz = math.floor(_CPR_BITS * ((lon % dlon) / dlon) + 0.5)
    return yz % _CPR_BITS, xz % _CPR_BITS


def quantized_alt_ft(alt_m: float) -> int:
    """Altitude as a Q=1 (25 ft) Mode S code carries it."""
    return int(round((alt_m / FT_TO_M + 1000) / 25)) * 25 - 1000


def _ac12(alt_ft: int) -> int:
    n = (alt_ft + 1000) // 25
    return ((n >> 4) << 5) | 0x10 | (n & 0xF)


def _df17(icao: str, me: int) -> str:
    return with_crc(f"8d{icao.lower()}{me:014x}000000")


def position_frame(icao: str, lat: float, lon: float, alt_m: float, odd: int) -> str:
    """DF17 TC11 airborne position (barometric altitude) with valid parity."""
    yz, xz = cpr_encode(lat, lon, odd)
    me = (
        (11 << 51)
        | (_ac12(quantized_alt_ft(alt_m)) << 36)
        | (odd << 34)
        | (yz << 17)
        | xz
    )
    return _df17(icao, me)


def velocity_frame(
    icao: str, speed_kmh: float, track_deg: float, vrate_mps: float = 0.0
) -> str:
    """DF17 TC19 subtype 1 (ground speed) airborne velocity."""
    speed_kt = speed_kmh / KT_TO_KMH
    vx = speed_kt * math.sin(math.radians(track_deg))
    vy = speed_kt * math.cos(math.radians(track_deg))
    vr_fpm = vrate_mps / FT_TO_M * 60
    vew = min(1023, int(round(abs(vx))) + 1)
    vns = min(1023, int(round(abs(vy))) + 1)
    vr = min(511, int(round(abs(vr_fpm) / 64)) + 1)
    me = (
        (19 << 51)
        | (1 << 48)
        | (int(vx < 0) << 42)
        | (vew << 32)
        | (int(vy < 0) << 31)
        | (vns << 21)
        | (int(vr_fpm < 0) << 19)
        | (vr << 10)
    )
    return _df17(icao, me)


def ident_frame(icao: str, callsign: str, category: int = 3) -> str:
    """DF17 TC4 aircraft identification."""
    chars = 0
    for ch in callsign.upper().ljust(8)[:8]:
        chars = (chars << 6) | _CALLSIGN_CHARSET.index(ch)
    me = (4 << 51) | (category << 48) | chars
    return _df17(icao, me)


def expected_speed_kmh(speed_kmh: float, track_deg: float) -> float:
    """Ground speed after the 1 kt quantisation of the velocity components."""
    speed_kt = speed_kmh / KT_TO_KMH
    vx = round(abs(speed_kt * math.sin(math.radians(track_deg))))
    vy = round(abs(speed_kt * math.cos(math.radians(track_deg))))
    return math.hypot(vx, vy) * KT_TO_KMH


# --- dump1090 Beast wire format (independent encoder / strict decoder) --------

BEAST_ESC = 0x1A
_BEAST_TYPES = {7: 0x32, 14: 0x33}
_BEAST_LENGTHS = {0x31: 2, 0x32: 7, 0x33: 14}


def beast_frame(hex_msg: str, clock: int, level: int) -> bytes:
    """One Beast frame; every 0x1a in clock, signal or payload is doubled."""
    payload = bytes.fromhex(hex_msg)
    body = (clock % (1 << 48)).to_bytes(6, "big") + bytes([level]) + payload
    escaped = body.replace(b"\x1a", b"\x1a\x1a")
    return bytes([BEAST_ESC, _BEAST_TYPES[len(payload)]]) + escaped


@dataclass
class StrictBeastFrame:
    hex: str
    clock: int
    level: int
    escapes: int


def strict_beast_decode(data: bytes) -> list[StrictBeastFrame]:
    """Decode a Beast byte stream, failing on any framing or escaping error.

    Unlike a tolerant parser this does not resync: a lone 0x1a inside a frame
    (an unescaped data byte) or garbage between frames is an AssertionError.
    A trailing partial frame (stream cut mid-frame) is ignored.
    """
    frames: list[StrictBeastFrame] = []
    i = 0
    n = len(data)
    while i < n:
        assert data[i] == BEAST_ESC, (
            f"byte {i}: expected 0x1a frame start, got {data[i]:#x}"
        )
        if i + 1 >= n:
            break
        kind = data[i + 1]
        assert kind in _BEAST_LENGTHS, f"byte {i + 1}: bad Beast type {kind:#x}"
        want = 6 + 1 + _BEAST_LENGTHS[kind]
        body = bytearray()
        j = i + 2
        escapes = 0
        truncated = False
        while len(body) < want:
            if j >= n:
                truncated = True
                break
            byte = data[j]
            if byte == BEAST_ESC:
                if j + 1 >= n:
                    truncated = True
                    break
                assert data[j + 1] == BEAST_ESC, (
                    f"byte {j}: unescaped 0x1a in a frame (next {data[j + 1]:#x})"
                )
                escapes += 1
                j += 2
            else:
                j += 1
            body.append(byte)
        if truncated:
            break
        frames.append(
            StrictBeastFrame(
                hex=bytes(body[7:]).hex(),
                clock=int.from_bytes(body[:6], "big"),
                level=body[6],
                escapes=escapes,
            )
        )
        i = j
    return frames


# --- Redis -------------------------------------------------------------------


def _score_bound(value: Any) -> tuple[float, bool]:
    """(score, exclusive) from a redis-py ZRANGEBYSCORE bound."""
    if isinstance(value, (int, float)):
        return float(value), False
    text = str(value).strip()
    exclusive = text.startswith("(")
    if exclusive:
        text = text[1:]
    if text in ("-inf", "-"):
        return -math.inf, exclusive
    if text in ("+inf", "inf", "+"):
        return math.inf, exclusive
    return float(text), exclusive


class FakePubSub:
    def __init__(self, server: E2ERedis) -> None:
        self._server = server
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._channels: set[str] = set()
        self.closed = False

    def subscribe(self, *channels: str) -> None:
        self._server.check_up()
        for channel in channels:
            self._channels.add(channel)
            self._server.add_subscriber(channel, self._queue)

    def get_message(
        self, timeout: float = 0.0, **_kwargs: Any
    ) -> dict[str, Any] | None:
        self._server.check_up()
        try:
            return self._queue.get(timeout=max(0.0, timeout or 0.0))
        except queue.Empty:
            return None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for channel in self._channels:
            self._server.remove_subscriber(channel, self._queue)


class E2EPipeline(FakePipeline):
    def execute(self) -> list[Any]:
        with self.client.lock:
            return super().execute()


class E2ERedis(FakeRedis):
    """tests/fake_redis.FakeRedis made thread safe, with pub/sub and the few
    extra commands the live store uses. Engine, portal and the portal's
    pub/sub listener all share one instance, like three clients of one
    Redis server."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.RLock()
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self.published: dict[str, int] = {}

    def check_up(self) -> None:
        self._hit("pubsub-io")

    # pub/sub
    def pubsub(
        self, ignore_subscribe_messages: bool = False, **_kwargs: Any
    ) -> FakePubSub:
        self._hit("pubsub")
        return FakePubSub(self)

    def add_subscriber(self, channel: str, q: queue.Queue) -> None:
        with self.lock:
            self._subscribers.setdefault(channel, []).append(q)

    def remove_subscriber(self, channel: str, q: queue.Queue) -> None:
        with self.lock, contextlib.suppress(ValueError):
            self._subscribers.get(channel, []).remove(q)

    def publish(self, channel: str, data: str) -> int:
        with self.lock:
            self._hit("publish")
            self.published[channel] = self.published.get(channel, 0) + 1
            targets = list(self._subscribers.get(channel, []))
        for q in targets:
            q.put({"type": "message", "channel": channel, "data": data})
        return len(targets)

    # extra commands
    def ttl(self, key: str) -> int:
        with self.lock:
            self._hit("ttl")
            if not any(
                key in store for store in (self.kv, self.sets, self.hashes, self.zsets)
            ):
                return -2
            ttl = self.ttls.get(key)
            return -1 if ttl is None else int(ttl)

    def _by_score(self, key: str, low: Any, high: Any) -> list[str]:
        lo, lo_ex = _score_bound(low)
        hi, hi_ex = _score_bound(high)
        out = []
        for member, score in self._sorted(key):
            if score < lo or (lo_ex and score == lo):
                continue
            if score > hi or (hi_ex and score == hi):
                continue
            out.append(member)
        return out

    @staticmethod
    def _page(items: list[str], start: int | None, num: int | None) -> list[str]:
        if start is None and num is None:
            return items
        start = start or 0
        return items[start:] if num is None or num < 0 else items[start : start + num]

    def zrangebyscore(  # type: ignore[override]
        self,
        key: str,
        min: Any,  # noqa: A002 - redis-py parameter names
        max: Any,  # noqa: A002
        start: int | None = None,
        num: int | None = None,
        **_kwargs: Any,
    ) -> list[str]:
        with self.lock:
            self._hit("zrangebyscore")
            return self._page(self._by_score(key, min, max), start, num)

    def zrevrangebyscore(
        self,
        key: str,
        max: Any,  # noqa: A002
        min: Any,  # noqa: A002
        start: int | None = None,
        num: int | None = None,
        **_kwargs: Any,
    ) -> list[str]:
        with self.lock:
            self._hit("zrevrangebyscore")
            return self._page(list(reversed(self._by_score(key, min, max))), start, num)

    def pipeline(self, transaction: bool = True) -> E2EPipeline:  # type: ignore[override]
        self._hit("pipeline")
        return E2EPipeline(self)


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: E2ERedis, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)

    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


for _name, _value in list(vars(FakeRedis).items()):
    if callable(_value) and not _name.startswith("_") and _name not in vars(E2ERedis):
        setattr(E2ERedis, _name, _locked(_value))


def install_fake_redis(monkeypatch: Any) -> E2ERedis:
    """Every ``redis.Redis.from_url`` (engine, portal, pub/sub) gets one server."""
    server = E2ERedis()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: server))
    return server


# --- external HTTP -------------------------------------------------------------


class _CannedResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        return None


@dataclass
class ExternalHttp:
    """Record of outbound calls to public services (all answered locally)."""

    hexdb: dict[str, dict] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def install_fake_external_http(
    monkeypatch: Any, hexdb: dict[str, dict] | None = None
) -> ExternalHttp:
    """Answer HexDB / Planespotters lookups locally; block any other remote GET.

    Loopback URLs (the test's own receiver.json server) go through for real.
    """
    state = ExternalHttp(hexdb={k.lower(): v for k, v in (hexdb or {}).items()})
    real_get = requests.get

    def fake_get(url: str, *args: Any, **kwargs: Any) -> Any:
        host = (urlsplit(url).hostname or "").lower()
        if host in (LOOPBACK, "localhost"):
            return real_get(url, *args, **kwargs)
        state.calls.append(url)
        if host == "hexdb.io":
            icao = url.rstrip("/").rsplit("/", 1)[-1].lower()
            record = state.hexdb.get(icao)
            return _CannedResponse(200, record) if record else _CannedResponse(404, {})
        if host == "api.planespotters.net":
            return _CannedResponse(200, {"photos": []})
        state.blocked.append(url)
        raise requests.ConnectionError("external network is disabled in e2e tests")

    monkeypatch.setattr(requests, "get", fake_get)
    return state


class _QuickHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup
        # that can stall for tens of seconds on a machine without a resolver.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)


class HttpSink:
    """Local HTTP server: records webhook POST/PUT bodies, serves receiver.json."""

    def __init__(self, receiver_json: dict | None = None) -> None:
        self.receiver_json = receiver_json
        self.posts: list[tuple[float, str, dict]] = []
        self.lock = threading.Lock()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"null")
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                with sink.lock:
                    sink.posts.append((_real_monotonic(), self.path, body))
                self.send_response(204)
                self.end_headers()

            do_POST = _record  # noqa: N815 - http.server naming
            do_PUT = _record  # noqa: N815

            def do_GET(self) -> None:  # noqa: N802
                if (
                    self.path == "/data/receiver.json"
                    and sink.receiver_json is not None
                ):
                    data = json.dumps(sink.receiver_json).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_args: Any) -> None:
                return None

        self.server = _QuickHTTPServer((LOOPBACK, 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://{LOOPBACK}:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def timed_bodies(self) -> list[tuple[float, dict]]:
        with self.lock:
            return [(t, body) for t, _p, body in self.posts]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(5)


# --- dump1090 Beast feeder -------------------------------------------------------


@dataclass(frozen=True)
class FeedEvent:
    """One frame *receiver* hears *at* seconds (real time) after playback starts."""

    at: float
    receiver: str
    hex: str
    clock: int
    level: int


@dataclass(frozen=True)
class SentFrame:
    receiver: str
    hex: str
    clock: int
    level: int
    connection: int


class BeastFeeder:
    """dump1090-style Beast TCP servers, one listening port per receiver.

    ``play`` streams a schedule of frames in real time. ``disconnect`` drops a
    receiver's connection at a scheduled moment; frames due while it is down
    are lost (a real feeder does not buffer for absent clients) and recorded
    in ``dropped``. Reconnections are accepted at any time.
    """

    def __init__(self, names: Iterable[str]) -> None:
        self.names = list(names)
        self._listeners: dict[str, socket.socket] = {}
        self.ports: dict[str, int] = {}
        self._conns: dict[str, socket.socket | None] = {}
        self.accepts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self.sent: list[SentFrame] = []
        self.dropped: list[FeedEvent] = []
        self.disconnects: dict[str, int] = {}
        self._threads: list[threading.Thread] = []
        for name in self.names:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((LOOPBACK, 0))
            sock.listen(8)
            sock.settimeout(0.1)
            self._listeners[name] = sock
            self.ports[name] = sock.getsockname()[1]
            self._conns[name] = None
            self.accepts[name] = 0
            thread = threading.Thread(
                target=self._accept_loop, args=(name,), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _accept_loop(self, name: str) -> None:
        listener = self._listeners[name]
        while not self._closed.is_set():
            try:
                conn, _addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                old = self._conns.get(name)
                self._conns[name] = conn
                self.accepts[name] += 1
            if old is not None:
                with contextlib.suppress(OSError):
                    old.close()

    def wait_connected(
        self,
        names: Iterable[str] | None = None,
        *,
        count: int = 1,
        timeout: float = 10.0,
    ) -> None:
        wanted = list(names or self.names)
        wait_for(
            lambda: all(self.accepts[n] >= count for n in wanted),
            timeout,
            message=f"receivers {wanted} to connect ({count}x)",
        )

    def _drop_connection(self, name: str) -> None:
        with self._lock:
            conn = self._conns.get(name)
            self._conns[name] = None
            self.disconnects[name] = self.disconnects.get(name, 0) + 1
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()

    def play(
        self,
        events: list[FeedEvent],
        *,
        disconnect: dict[str, float] | None = None,
    ) -> threading.Thread:
        """Stream *events* in a background thread; returns it (join to wait)."""
        schedule = sorted(events, key=lambda e: e.at)
        pending_disconnects = sorted((t, n) for n, t in (disconnect or {}).items())

        def run() -> None:
            start = _real_monotonic()
            index = 0
            while index < len(schedule) and not self._closed.is_set():
                now = _real_monotonic() - start
                while pending_disconnects and pending_disconnects[0][0] <= now:
                    self._drop_connection(pending_disconnects.pop(0)[1])
                batch: dict[str, list[FeedEvent]] = {}
                while index < len(schedule) and schedule[index].at <= now:
                    event = schedule[index]
                    batch.setdefault(event.receiver, []).append(event)
                    index += 1
                for name, items in batch.items():
                    self._send(name, items)
                if index < len(schedule):
                    delay = schedule[index].at - (_real_monotonic() - start)
                    if delay > 0:
                        self._closed.wait(min(delay, 0.02))

        thread = threading.Thread(target=run, name="beast-feeder", daemon=True)
        thread.start()
        return thread

    def _send(self, name: str, items: list[FeedEvent]) -> None:
        with self._lock:
            conn = self._conns.get(name)
            connection = self.accepts[name]
        if conn is None:
            self.dropped.extend(items)
            return
        data = b"".join(beast_frame(e.hex, e.clock, e.level) for e in items)
        try:
            conn.sendall(data)
        except OSError:
            with self._lock:
                if self._conns.get(name) is conn:
                    self._conns[name] = None
            self.dropped.extend(items)
            return
        self.sent.extend(
            SentFrame(e.receiver, e.hex, e.clock, e.level, connection) for e in items
        )

    def close(self) -> None:
        self._closed.set()
        for name in self.names:
            self._drop_connection(name)
            with contextlib.suppress(OSError):
                self._listeners[name].close()
        for thread in self._threads:
            thread.join(2)


# --- engine / portal processes ---------------------------------------------------


def write_config(path: Path, data: dict) -> Path:
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    return path


class EngineThread:
    """Runs ``Engine.run()`` (the ``pyaerial run`` main loop) in a thread.

    ``stop`` sets the engine's shutdown event, exactly what its SIGINT/SIGTERM
    handler does, so the loop exits and ``run()`` performs its own graceful
    shutdown (alerts closed, flights archived, stores closed).
    """

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="e2e-engine", daemon=True)

    def _run(self) -> None:
        try:
            self.engine.run()
        except BaseException as exc:  # surfaced by stop()
            self.error = exc

    def start(self) -> EngineThread:
        self.thread.start()
        return self

    def stop(self, timeout: float = 20.0) -> None:
        self.engine._shutdown.set()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "engine did not shut down"
        if self.error is not None:
            raise self.error


class PortalServer:
    """The FastAPI portal served by a real uvicorn server on 127.0.0.1:0."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((LOOPBACK, 0))
        self.port = self._sock.getsockname()[1]
        self.http = f"http://{LOOPBACK}:{self.port}"
        self.ws = f"ws://{LOOPBACK}:{self.port}"
        config = uvicorn.Config(
            app,
            log_level="warning",
            lifespan="on",
            ws_max_size=64 * 1024,
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self._sock]}, daemon=True
        )

    def start(self) -> PortalServer:
        self._thread.start()
        wait_for(lambda: self.server.started, 10, message="uvicorn startup")
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(10)
        with contextlib.suppress(OSError):
            self._sock.close()


class WsCollector:
    """Background reader recording every message from one WebSocket."""

    def __init__(self, url: str, *, binary: bool = False) -> None:
        from websockets.sync.client import connect

        self.binary = binary
        self.messages: list[tuple[float, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stack = contextlib.ExitStack()
        self.ws = self._stack.enter_context(
            connect(url, max_size=None, open_timeout=5, close_timeout=2)
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from websockets.exceptions import ConnectionClosed

        while not self._stop.is_set():
            try:
                raw = self.ws.recv(timeout=0.2)
            except TimeoutError:
                continue
            except ConnectionClosed:
                return
            item = raw if self.binary else json.loads(raw)
            with self._lock:
                self.messages.append((_real_monotonic(), item))

    def snapshot(self) -> list[Any]:
        with self._lock:
            return [item for _t, item in self.messages]

    def timed(self) -> list[tuple[float, Any]]:
        with self._lock:
            return list(self.messages)

    def wait(
        self, predicate: Callable[[list[Any]], Any], timeout: float, message: str
    ) -> Any:
        return wait_for(lambda: predicate(self.snapshot()), timeout, message=message)

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            self._stack.close()
        self._thread.join(3)


def ws_rpc(
    base_ws: str, action: str, params: dict | None = None, *, timeout: float = 10.0
) -> dict:
    """One RPC on a fresh ``/ws/live`` connection, as the portal frontend sends it."""
    from websockets.sync.client import connect

    req_id = f"e2e-{action}"
    with connect(
        f"{base_ws}/ws/live?streams=stats", max_size=None, open_timeout=5
    ) as ws:
        hello = json.loads(ws.recv(timeout=timeout))
        assert hello["type"] == "hello", hello
        ws.send(
            json.dumps(
                {
                    "type": "request",
                    "id": req_id,
                    "action": action,
                    "params": params or {},
                }
            )
        )
        deadline = _real_monotonic() + timeout
        while True:
            remaining = deadline - _real_monotonic()
            assert remaining > 0, f"no response to {action}"
            message = json.loads(ws.recv(timeout=remaining))
            if message.get("type") == "response" and message.get("id") == req_id:
                return message


class TcpCollector:
    """Background reader of a raw TCP byte stream (the Beast re-serve port)."""

    def __init__(self, host: str, port: int) -> None:
        self.data = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self.data.extend(chunk)

    def received(self) -> bytes:
        with self._lock:
            return bytes(self.data)

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self.sock.close()
        self._thread.join(3)


class FastClock:
    """``time.time`` replacement running *factor* times faster than real time.

    Lets the 30 s integrity trust window and receiver clock-sync fitting elapse
    in a few real seconds. Everything that reads wall time through
    ``time.time()`` (receivers, tracker, integrity, alerts, stores) sees one
    consistent accelerated clock; loop pacing still uses ``time.monotonic``.
    """

    def __init__(self, factor: float) -> None:
        self.factor = factor
        self.t0 = _real_time()
        self.m0 = _real_monotonic()

    def __call__(self) -> float:
        return self.t0 + (_real_monotonic() - self.m0) * self.factor


# --- archive inspection -------------------------------------------------------------


def query(
    db_path: Path | str, sql: str, params: Iterable[Any] = ()
) -> list[sqlite3.Row]:
    """Read-only query against the archive (safe while the engine writes: WAL)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()


def count(db_path: Path | str, sql: str, params: Iterable[Any] = ()) -> int:
    try:
        return int(query(db_path, sql, params)[0][0])
    except sqlite3.OperationalError:
        return 0
