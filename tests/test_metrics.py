"""GET /metrics: Prometheus text exposition format 0.0.4."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState

from helpers import make_config
from pyaerial import __version__
from pyaerial.api import beast
from pyaerial.api import broadcaster as bc
from pyaerial.api.app import create_app
from pyaerial.api.beast import BeastHub
from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.metrics import (
    CONTENT_TYPE,
    Backends,
    Exposition,
    PortalCounts,
    probe_backends,
    render,
)
from pyaerial.config.schema import ReceiverConfig
from pyaerial.store.live import LiveReader, LiveWriter

# --- A small strict parser for the exposition format --------------------------

_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}


@dataclass
class Family:
    kind: str
    help: str
    samples: dict[frozenset, float] = field(default_factory=dict)


def _unescape_help(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            nxt = text[i + 1]
            assert nxt in ("\\", "n"), f"bad HELP escape \\{nxt}"
            out.append(_ESCAPES[nxt])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_sample(line: str) -> tuple[str, dict[str, str], float]:
    match = _NAME_RE.match(line)
    assert match, f"bad sample line {line!r}"
    name, i = match.group(), match.end()
    labels: dict[str, str] = {}
    if line[i] == "{":
        i += 1
        while line[i] != "}":
            key = _LABEL_RE.match(line, i)
            assert key, f"bad label name in {line!r}"
            assert key.group() not in labels, f"duplicate label in {line!r}"
            i = key.end()
            assert line[i : i + 2] == '="', f'expected =" in {line!r}'
            i += 2
            value: list[str] = []
            while line[i] != '"':
                if line[i] == "\\":
                    nxt = line[i + 1]
                    assert nxt in _ESCAPES, f"bad label escape \\{nxt} in {line!r}"
                    value.append(_ESCAPES[nxt])
                    i += 2
                else:
                    value.append(line[i])
                    i += 1
            labels[key.group()] = "".join(value)
            i += 1
            if line[i] == ",":
                i += 1
            else:
                assert line[i] == "}", f"expected , or }} in {line!r}"
        i += 1
    assert line[i] == " ", f"expected one space before the value in {line!r}"
    text = line[i + 1 :]
    assert " " not in text, f"unexpected timestamp or junk in {line!r}"
    if text in ("NaN", "+Inf", "-Inf"):
        return name, labels, float(text.lstrip("+"))
    assert re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", text), f"bad value {text!r}"
    return name, labels, float(text)


def parse_exposition(text: str) -> dict[str, Family]:
    assert text.endswith("\n"), "exposition must end with a newline"
    families: dict[str, Family] = {}
    helps: dict[str, str] = {}
    current: str | None = None
    for line in text[:-1].split("\n"):
        assert line, "blank line"
        if line.startswith("# HELP "):
            name, sep, help_text = line[len("# HELP ") :].partition(" ")
            assert sep and _NAME_RE.fullmatch(name), f"bad HELP line {line!r}"
            assert name not in helps, f"second HELP for {name}"
            helps[name] = _unescape_help(help_text)
        elif line.startswith("# TYPE "):
            parts = line[len("# TYPE ") :].split(" ")
            assert len(parts) == 2, f"bad TYPE line {line!r}"
            name, kind = parts
            assert _NAME_RE.fullmatch(name), f"bad metric name {name!r}"
            assert name.startswith("pyaerial_"), name
            assert kind in ("counter", "gauge"), kind
            assert name not in families, f"second TYPE for {name}"
            assert name in helps, f"TYPE without HELP for {name}"
            if kind == "counter":
                assert name.endswith("_total"), f"counter {name} must end in _total"
            families[name] = Family(kind, helps[name])
            current = name
        elif line.startswith("#"):
            raise AssertionError(f"unexpected comment {line!r}")
        else:
            name, labels, value = _parse_sample(line)
            assert name == current, f"sample {name} without a preceding TYPE"
            key = frozenset(labels.items())
            assert key not in families[name].samples, f"duplicate sample {line!r}"
            families[name].samples[key] = value
    for name in helps:
        assert name in families, f"HELP without TYPE for {name}"
    return families


def value(families: dict[str, Family], name: str, **labels: str) -> float:
    return families[name].samples[frozenset(labels.items())]


# --- Fixtures -----------------------------------------------------------------

ODD_RECEIVER = 'odd "name"\\with\nnewline'


def _heartbeat(seen_at: float) -> dict:
    return {
        "seen_at": seen_at,
        "token": "tok-secret-0123",
        "history": "ok",
        "history_error": "/very/secret/archive.db",
        "history_pending": 2,
        "counters": {
            "since": 1_700_000_000.5,
            "frames_received": 12345,
            "frames_dropped": 7,
            "messages_processed": 12000,
            "planes_tracked": 9,
            "pending_evicted": 1,
            "receiver_restarts": {"main": 2, ODD_RECEIVER: 1},
            "stage_errors": {"tracking": 3},
        },
    }


def _config(**web):
    config = make_config(
        receivers={
            "main": ReceiverConfig(type="dump1090", host="192.0.2.55", port=30005),
            "spare": ReceiverConfig(type="dump1090", host="198.51.100.7"),
        }
    )
    for key, val in web.items():
        setattr(config.web, key, val)
    return config


class _History:
    def __init__(self, up: bool = True) -> None:
        self.up = up

    def ping(self) -> bool:
        return self.up


@pytest.fixture
def store(fake) -> LiveReader:
    return LiveReader("redis://:hunter2@localhost:6379/0")


def _scrape(app, **kwargs) -> tuple[str, dict[str, Family]]:
    # No lifespan: the broadcaster and Beast hub need not run to be read.
    response = TestClient(app).get("/metrics", **kwargs)
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    return response.text, parse_exposition(response.text)


# --- Tests --------------------------------------------------------------------


def test_values_match_the_heartbeat(fake, store):
    fake.kv["live:engine"] = json.dumps(_heartbeat(time.time()))
    app = create_app(config=_config(), history=_History(), live_store=store)
    text, fams = _scrape(app)

    assert value(fams, "pyaerial_build_info", version=__version__) == 1
    assert value(fams, "pyaerial_redis_up") == 1
    assert value(fams, "pyaerial_archive_up") == 1
    assert value(fams, "pyaerial_engine_up") == 1
    assert value(fams, "pyaerial_engine_history_up") == 1
    assert value(fams, "pyaerial_engine_start_time_seconds") == 1_700_000_000.5
    assert value(fams, "pyaerial_engine_frames_received_total") == 12345
    assert value(fams, "pyaerial_engine_frames_dropped_total") == 7
    assert value(fams, "pyaerial_engine_messages_processed_total") == 12000
    assert value(fams, "pyaerial_engine_pending_evicted_total") == 1
    assert value(fams, "pyaerial_engine_planes_tracked") == 9
    assert value(fams, "pyaerial_engine_history_pending") == 2
    restarts = "pyaerial_engine_receiver_restarts_total"
    assert value(fams, restarts, receiver="main") == 2
    assert value(fams, restarts, receiver="spare") == 0  # configured, never restarted
    assert value(fams, restarts, receiver=ODD_RECEIVER) == 1  # escaped and back
    assert value(fams, "pyaerial_engine_stage_errors_total", stage="tracking") == 3
    for endpoint in ("/ws/live", "/ws/raw", "/ws/beast"):
        assert value(fams, "pyaerial_websocket_connections", endpoint=endpoint) == 0
    assert value(fams, "pyaerial_beast_tcp_clients") == 0
    assert value(fams, "pyaerial_websocket_queued_bytes") == 0
    assert fams["pyaerial_engine_planes_tracked"].kind == "gauge"
    assert fams["pyaerial_engine_frames_received_total"].kind == "counter"

    for secret in (
        "hunter2",
        "192.0.2.55",
        "198.51.100.7",
        "30005",
        "tok-secret",
        "secret/archive",
    ):
        assert secret not in text


@pytest.mark.parametrize("heartbeat", ["stale", "missing"])
def test_engine_down_reports_up_0_without_engine_counters(fake, store, heartbeat):
    if heartbeat == "stale":
        fake.kv["live:engine"] = json.dumps(_heartbeat(time.time() - 60))
    app = create_app(config=_config(), history=_History(), live_store=store)
    _text, fams = _scrape(app)
    assert value(fams, "pyaerial_engine_up") == 0
    assert value(fams, "pyaerial_redis_up") == 1
    assert [name for name in fams if name.startswith("pyaerial_engine_")] == [
        "pyaerial_engine_up"
    ]


def test_redis_outage_still_answers_200(fake, store):
    fake.kv["live:engine"] = json.dumps(_heartbeat(time.time()))
    fake.down = True
    app = create_app(config=_config(), history=_History(), live_store=store)
    _text, fams = _scrape(app)
    assert value(fams, "pyaerial_redis_up") == 0
    assert value(fams, "pyaerial_engine_up") == 0
    assert value(fams, "pyaerial_archive_up") == 1
    assert "pyaerial_engine_frames_received_total" not in fams


def test_archive_up_follows_the_history_ping(fake, store):
    app = create_app(config=_config(), history=_History(up=False), live_store=store)
    _text, fams = _scrape(app)
    assert value(fams, "pyaerial_archive_up") == 0
    app = create_app(config=_config(), history=None, live_store=None)
    _text, fams = _scrape(app)
    assert value(fams, "pyaerial_archive_up") == 0
    assert value(fams, "pyaerial_redis_up") == 0
    assert value(fams, "pyaerial_engine_up") == 0


def test_one_redis_get_per_scrape(fake, store):
    fake.kv["live:engine"] = json.dumps(_heartbeat(time.time()))
    store.ping()
    fake.calls.clear()
    backends = probe_backends(store, _History())
    assert fake.calls.count("get") == 1
    assert set(fake.calls) <= {"get", "ping"}
    assert backends.heartbeat is not None and backends.heartbeat["history_pending"] == 2


def test_memory_only_store_uses_engine_seen_at():
    store = LiveWriter("redis://localhost:6379/0", memory_only=True)
    app = create_app(config=_config(), history=None, live_store=store)
    _text, fams = _scrape(app)
    assert value(fams, "pyaerial_engine_up") == 0
    store.touch_engine()
    _text, fams = _scrape(app)
    assert value(fams, "pyaerial_engine_up") == 1


def test_websocket_connections_by_endpoint():
    app = create_app(config=_config(), history=None, live_store=None)
    client = TestClient(app)
    with client.websocket_connect("/ws/raw"), client.websocket_connect("/ws/beast"):
        fams = parse_exposition(client.get("/metrics").text)
        assert value(fams, "pyaerial_websocket_connections", endpoint="/ws/raw") == 1
        assert value(fams, "pyaerial_websocket_connections", endpoint="/ws/beast") == 1
        assert value(fams, "pyaerial_websocket_connections", endpoint="/ws/live") == 0
    with TestClient(app) as running, running.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        fams = parse_exposition(running.get("/metrics").text)
        assert value(fams, "pyaerial_websocket_connections", endpoint="/ws/live") == 1
    fams = parse_exposition(TestClient(app).get("/metrics").text)
    assert sum(fams["pyaerial_websocket_connections"].samples.values()) == 0


def test_browser_origins_and_hosts_follow_the_portal_rules():
    app = create_app(config=_config(allowed_hosts=["tracker.example"]), live_store=None)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert (
        client.get("/metrics", headers={"origin": "https://evil.example"}).status_code
        == 403
    )
    same = client.get(
        "/metrics",
        headers={"origin": "http://tracker.example", "host": "tracker.example"},
    )
    assert same.status_code == 200
    rebind = TestClient(app, base_url="http://rebind.attacker.example")
    assert rebind.get("/metrics").status_code == 400


def test_metrics_can_be_disabled():
    app = create_app(config=_config(metrics=False), live_store=None)
    response = TestClient(app).get("/metrics")
    assert response.status_code == 404
    assert "pyaerial_" not in response.text


def test_render_rejects_garbage_heartbeat_values():
    now = time.time()
    doc = {
        "seen_at": now,
        "history": "disabled",
        "history_pending": "2",
        "counters": {
            "since": True,
            "frames_received": float("nan"),
            "frames_dropped": None,
            "planes_tracked": 3.0,
            "receiver_restarts": {"main": "x", "b": 1.5},
            "stage_errors": [],
        },
    }
    text = render(
        backends=Backends(True, True, doc, now),
        portal=PortalCounts({"/ws/live": 2}, 1, 10),
        receivers=["main"],
        version='1.0 "rc"\\1',
    )
    fams = parse_exposition(text)
    assert value(fams, "pyaerial_build_info", version='1.0 "rc"\\1') == 1
    assert value(fams, "pyaerial_engine_history_up") == 0
    assert value(fams, "pyaerial_engine_planes_tracked") == 3
    assert value(fams, "pyaerial_engine_receiver_restarts_total", receiver="main") == 0
    assert value(fams, "pyaerial_engine_receiver_restarts_total", receiver="b") == 1.5
    for absent in (
        "pyaerial_engine_start_time_seconds",
        "pyaerial_engine_frames_received_total",
        "pyaerial_engine_frames_dropped_total",
        "pyaerial_engine_history_pending",
        "pyaerial_engine_stage_errors_total",
    ):
        assert absent not in fams
    assert value(fams, "pyaerial_websocket_connections", endpoint="/ws/live") == 2
    assert value(fams, "pyaerial_websocket_connections", endpoint="/ws/raw") == 0
    assert value(fams, "pyaerial_beast_tcp_clients") == 1
    assert value(fams, "pyaerial_websocket_queued_bytes") == 10


def test_exposition_guards_names_and_formats_values():
    out = Exposition()
    with pytest.raises(ValueError):
        out.add("pyaerial_bad_counter", "counter", "x", [({}, 1)])
    with pytest.raises(ValueError):
        out.add("pyaerial-dash", "gauge", "x", [({}, 1)])
    with pytest.raises(ValueError):
        out.add("other_prefix", "gauge", "x", [({}, 1)])
    with pytest.raises(ValueError):
        out.gauge("pyaerial_label", "x", 1, **{"bad-label": "v"})
    out.add("pyaerial_empty", "gauge", "no samples, no family", [])
    out.add(
        "pyaerial_values",
        "gauge",
        "back\\slash and\nnewline",
        [
            ({"v": "a"}, math.inf),
            ({"v": "b"}, -math.inf),
            ({"v": "c"}, math.nan),
            ({"v": "d"}, 0.25),
        ],
    )
    fams = parse_exposition(out.text())
    assert "pyaerial_empty" not in fams
    assert fams["pyaerial_values"].help == "back\\slash and\nnewline"
    assert value(fams, "pyaerial_values", v="a") == math.inf
    assert value(fams, "pyaerial_values", v="b") == -math.inf
    assert math.isnan(value(fams, "pyaerial_values", v="c"))
    assert value(fams, "pyaerial_values", v="d") == 0.25


# --- Drop and rate-limit counters ----------------------------------------------


class _SlowWS:
    """A /ws/live peer whose sends never finish (a non-reader)."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        await self.release.wait()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def test_live_backpressure_drops_are_counted_by_reason(monkeypatch):
    monkeypatch.setattr(bc, "_SERVER_QUEUE_BYTES", 10_000)

    async def _scenario() -> tuple[dict, dict]:
        broadcaster = LiveBroadcaster(None, None)
        reader = _SlowWS()
        await broadcaster.connect(reader)
        # Responses are never evicted: one past the queue cap drops the client.
        for _ in range(bc._CLIENT_QUEUE_MAX + 2):
            broadcaster.send_response(reader, "{}")
        await asyncio.sleep(0)
        slow = broadcaster.dropped_clients
        hog, polite = _SlowWS(), _SlowWS()
        await broadcaster.connect(hog)
        await broadcaster.connect(polite)
        broadcaster.send_response(hog, "h" * 6000)
        broadcaster.send_response(hog, "h" * 3000)
        await asyncio.sleep(0)
        broadcaster.send_response(polite, "p" * 3000)  # over budget: hog goes
        await asyncio.sleep(0)
        both = broadcaster.dropped_clients
        await broadcaster.stop()
        return slow, both

    slow, both = asyncio.run(_scenario())
    assert slow == {"client_too_slow": 1}
    assert both == {"client_too_slow": 1, "server_budget": 1}


class _StuckFeedWS:
    """A /ws/raw peer whose first send blocks until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.client_state = WebSocketState.CONNECTED
        self.closed_with: int | None = None

    async def send_text(self, text: str) -> None:
        await self.release.wait()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = code
        self.client_state = WebSocketState.DISCONNECTED


def test_raw_feed_cut_is_counted(monkeypatch):
    monkeypatch.setattr(beast, "_OVERFLOW_GRACE_SECONDS", 0.0)

    async def _scenario() -> tuple[dict, int | None]:
        hub = BeastHub(None, port=None)
        ws = _StuckFeedWS()
        queue = hub._attach_json()
        writer = asyncio.create_task(hub._ws_writer(ws, queue, json_mode=True))
        for _ in range(beast._CLIENT_QUEUE_MAX + 3):
            hub._broadcast_json([{"hex": "8d"}])
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        hub._broadcast_json([{"hex": "8d"}])  # behind for longer than the grace
        assert queue.cut_off
        ws.release.set()
        await asyncio.wait_for(writer, 1)
        return hub.websocket_drops, ws.closed_with

    drops, code = asyncio.run(_scenario())
    assert drops == {"feed_behind": 1}
    assert code == 1013


class _Transport:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class _StalledWriter:
    """A Beast TCP StreamWriter whose peer never reads."""

    def __init__(self) -> None:
        self.transport = _Transport()

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        await asyncio.Event().wait()

    def get_extra_info(self, name: str) -> Any:
        return None

    def close(self) -> None:
        return None


def test_beast_tcp_drops_are_counted_by_reason(monkeypatch):
    monkeypatch.setattr(beast, "_DRAIN_TIMEOUT_SECONDS", 0.05)

    async def _scenario() -> tuple[dict, bool, bool]:
        hub = BeastHub(None, port=None)
        not_reading = _StalledWriter()
        queue = hub._attach()
        queue.put_nowait(b"\x1a")
        await hub._tcp_writer(not_reading, queue)
        behind = _StalledWriter()
        queue = hub._attach()
        queue.cut()
        await hub._tcp_writer(behind, queue)
        return hub.tcp_drops, not_reading.transport.aborted, behind.transport.aborted

    drops, aborted_1, aborted_2 = asyncio.run(_scenario())
    assert drops == {"not_reading": 1, "feed_behind": 1}
    assert aborted_1 and aborted_2


def test_rate_limited_requests_are_counted():
    config = _config()
    config.web.ws_rate_limit_per_ip = 0.01
    config.web.ws_rate_burst_per_ip = 2
    app = create_app(config=config, live_store=None)
    with TestClient(app) as client:
        fams = parse_exposition(client.get("/metrics").text)
        assert value(fams, "pyaerial_websocket_rate_limited_total") == 0
        with client.websocket_connect("/ws/live?streams=stats") as ws:
            ws.receive_json()
            codes = []
            for i in range(5):
                ws.send_json({"type": "request", "id": i, "action": "fetchZones"})
                while (reply := ws.receive_json()).get("type") != "response":
                    pass
                codes.append(reply.get("code"))
        fams = parse_exposition(client.get("/metrics").text)
    assert codes.count("rate_limited") == 3
    assert value(fams, "pyaerial_websocket_rate_limited_total") == 3
    assert fams["pyaerial_websocket_rate_limited_total"].kind == "counter"


def test_drop_counters_render_with_every_reason():
    portal = PortalCounts(
        {},
        0,
        0,
        websocket_drops={"client_too_slow": 2, "feed_behind": 1},
        beast_tcp_drops={"not_reading": 4},
        rate_limited=7,
    )
    fams = parse_exposition(
        render(backends=Backends(False, False, None, time.time()), portal=portal)
    )
    ws = "pyaerial_websocket_clients_dropped_total"
    assert fams[ws].kind == "counter"
    assert value(fams, ws, reason="client_too_slow") == 2
    assert value(fams, ws, reason="server_budget") == 0
    assert value(fams, ws, reason="feed_behind") == 1
    tcp = "pyaerial_beast_tcp_clients_dropped_total"
    assert value(fams, tcp, reason="not_reading") == 4
    assert value(fams, tcp, reason="feed_behind") == 0
    assert value(fams, "pyaerial_websocket_rate_limited_total") == 7
