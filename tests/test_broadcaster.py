"""LiveBroadcaster outbox, telemetry cursor and threading regressions."""

from __future__ import annotations

import asyncio
import json
import threading
import time

from pyaerial.api.broadcaster import (
    LiveBroadcaster,
    _Client,
    _flights_sig,
    _Outbox,
    _Queued,
)
from pyaerial.api.queries import get_tracked_live_alerts
from pyaerial.api.spec import ALERTS_PUSH_MIN


def _q(message: dict) -> _Queued:
    return _Queued(message["type"], json.dumps(message))


def test_outbox_never_evicts_responses():
    async def _scenario() -> None:
        box = _Outbox(maxsize=3)
        assert box.put(_q({"type": "response", "id": 1}))
        assert box.put(_q({"type": "flights"}))
        assert box.put(_q({"type": "response", "id": 2}))
        # Full: a new snapshot evicts the oldest non-response message.
        assert box.put(_q({"type": "alerts"}))
        assert box.types() == ["response", "response", "alerts"]
        # Low-value streams are skipped rather than evicting anything.
        assert not box.put(_q({"type": "telemetry"}))
        # A response may evict a stream snapshot...
        assert box.put(_q({"type": "response", "id": 3}))
        assert box.types() == ["response", "response", "response"]
        # ...but never another response: the client is stuck.
        assert not box.put(_q({"type": "response", "id": 4}))
        assert not box.put(_q({"type": "flights"}))
        assert json.loads((await box.get()).text)["id"] == 1

    asyncio.run(_scenario())


def test_outbox_get_waits_for_put():
    async def _scenario() -> dict:
        box = _Outbox(maxsize=2)
        getter = asyncio.create_task(box.get())
        await asyncio.sleep(0)
        box.put(_q({"type": "ping"}))
        return (await asyncio.wait_for(getter, timeout=1)).kind

    assert asyncio.run(_scenario()) == "ping"


def _point(flight: str, ts: float) -> dict:
    return {"flight_id": flight, "timestamp": ts, "latitude": 1.0, "longitude": 2.0}


def test_telemetry_cursor_is_per_flight():
    """A late write for one flight is not hidden by another flight's newer point."""
    client = _Client(telemetry_floor=100.0, last_ping=0.0)
    first = client.select_telemetry([_point("a", 101.0), _point("b", 110.0)])
    assert len(first) == 2
    client.mark_telemetry_sent(first)

    # Flight "a" lands a point stamped before flight "b"'s newest one.
    late = client.select_telemetry(
        [_point("a", 101.0), _point("a", 105.0), _point("b", 110.0)]
    )
    assert late == [_point("a", 105.0)]
    client.mark_telemetry_sent(late)
    assert client.select_telemetry([_point("a", 105.0), _point("b", 110.0)]) == []


def test_telemetry_floor_advances_and_prunes_old_cursors():
    client = _Client(telemetry_floor=0.0, last_ping=0.0)
    client.mark_telemetry_sent([_point("old", 10.0), _point("new", 100.0)])
    # Floor = newest - slack (30 s); cursors at or below it are dropped.
    assert client.telemetry_floor == 70.0
    assert "old" not in client.telemetry_marks
    assert client.telemetry_marks["new"] == 100.0
    # A brand-new flight still gets points newer than the floor.
    assert client.select_telemetry([_point("fresh", 75.0)]) == [_point("fresh", 75.0)]


class _Store:
    def __init__(self) -> None:
        self.since_calls: list[float] = []

    def get_flights(self):
        return [{"flight_id": "f1", "icao": "abc123", "timestamp": time.time()}]

    def count_flights(self):
        return len(self.get_flights())

    def get_alerts(self, **_kwargs):
        return []

    def get_live_telemetry(self, since=0.0):
        self.since_calls.append(since)
        return [_point("f1", since + 1)]

    def engine_is_live(self):
        return True

    def engine_seen_at(self):
        return time.time()

    def ping(self):
        return True


def test_collect_live_payload_uses_only_its_arguments():
    store = _Store()
    broadcaster = LiveBroadcaster(store, None)
    # Mutating the client map while a worker runs must not matter: the worker
    # receives the telemetry cursor and the stats decision as arguments.
    broadcaster._clients = None  # type: ignore[assignment]
    data = broadcaster._collect_live_payload(42.0, refresh_stats=True)
    assert store.since_calls == [42.0]
    assert data.points and data.points[0]["timestamp"] == 43.0
    assert data.stats is not None and data.stats["engine_live"] is True
    assert broadcaster._last_stats is None  # cache is written on the loop only
    no_tel = broadcaster._collect_live_payload(None, refresh_stats=False)
    assert no_tel.points == [] and no_tel.stats is None


def test_broadcast_tick_delivers_late_points_per_flight():
    store = _Store()

    async def _scenario() -> list[dict]:
        broadcaster = LiveBroadcaster(store, None)
        client = _Client(telemetry_floor=100.0, last_ping=time.time())
        client.telemetry_marks = {"f1": 200.0, "f2": 300.0}
        client.telemetry_floor = 150.0
        broadcaster._clients = {object(): client}  # type: ignore[dict-item]
        store.get_live_telemetry = lambda since=0.0: [  # type: ignore[method-assign]
            _point("f1", 250.0),
            _point("f2", 300.0),
        ]
        await broadcaster._broadcast_tick()
        assert store.since_calls == []
        return [m for m in client.outbox.messages() if m["type"] == "telemetry"]

    sent = asyncio.run(_scenario())
    assert len(sent) == 1
    assert sent[0]["telemetry"] == [_point("f1", 250.0)]


def test_stalled_client_does_not_pin_the_telemetry_window():
    """A client that stops reading must not make every tick re-read the backlog."""
    store = _Store()

    async def _scenario() -> None:
        broadcaster = LiveBroadcaster(store, None)
        stalled = _Client(telemetry_floor=100.0, last_ping=time.time())
        for i in range(stalled.outbox.maxsize):
            stalled.outbox.put(_q({"type": "response", "id": i}))
        broadcaster._clients = {object(): stalled}  # type: ignore[dict-item]

        def _telemetry(since=0.0):
            store.since_calls.append(since)
            return [_point("f1", since + 100)]

        store.get_live_telemetry = _telemetry  # type: ignore[method-assign]
        await broadcaster._broadcast_tick()
        await broadcaster._broadcast_tick()
        assert store.since_calls[0] == 100.0
        # The dropped batch is skipped (floor = newest - 30 s slack), not
        # re-fetched on every later tick.
        assert store.since_calls[1] == 170.0

    asyncio.run(_scenario())


def test_integrity_change_alone_pushes_flights():
    """A new verdict for a flight with no new position still reaches clients."""
    verdict = {"score": 1.0, "status": "ok", "checks": {}}

    class _FixedStore(_Store):
        def get_flights(self):
            return [
                {
                    "flight_id": "f1",
                    "icao": "abc123",
                    "timestamp": 100.0,
                    "integrity": dict(verdict),
                }
            ]

        def get_live_telemetry(self, since=0.0):
            return []

    async def _scenario() -> list[str]:
        broadcaster = LiveBroadcaster(_FixedStore(), None)
        client = _Client(telemetry_floor=0.0, last_ping=time.time())
        broadcaster._clients = {object(): client}  # type: ignore[dict-item]
        statuses = []
        for update in ({}, {"score": 0.2, "status": "spoofed"}):
            verdict.update(update)
            await broadcaster._broadcast_tick()
            # Drain like the writer would (queued snapshots coalesce).
            statuses += [
                item["flights"][0]["integrity"]["status"]
                for item in client.outbox.messages()
                if item["type"] == "flights"
            ]
            client.outbox.clear()
        return statuses

    assert asyncio.run(_scenario()) == ["ok", "spoofed"]


def test_pushed_alerts_keep_every_active_episode():
    class _AlertStore(_Store):
        def get_alerts(self, **_kwargs):
            return [
                {
                    "alert_id": f"a{i}",
                    "flight_id": "f1",
                    "zone": "z",
                    "rule": "r",
                    "active": True,
                    "activated_at": float(i),
                }
                for i in range(ALERTS_PUSH_MIN + 5)
            ]

    alerts = get_tracked_live_alerts(
        _AlertStore(), [{"flight_id": "f1"}], limit=ALERTS_PUSH_MIN
    )
    assert len(alerts) == ALERTS_PUSH_MIN + 5


# --- Ordering, change detection, telemetry subscribe and pings ---------------


class _RecordingWS:
    """Just enough of a WebSocket for the broadcaster."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _GatedStore:
    """Live store whose reads return the state at entry, optionally blocking.

    A read that starts before an update and finishes after it returns the old
    state, like a slow Redis round trip.
    """

    def __init__(self) -> None:
        self.callsign = "OLD"
        self.block_next = False
        self.gate = threading.Event()
        self.points: list[dict] = []
        self.telemetry_since: list[float] = []

    def get_flights(self):
        flights = [{"flight_id": "f1", "icao": "abc123", "callsign": self.callsign}]
        if self.block_next:
            self.block_next = False
            self.gate.wait(5)
        return flights

    def get_alerts(self, **_kwargs):
        return []

    def count_flights(self):
        return 1

    def get_live_telemetry(self, since=0.0):
        self.telemetry_since.append(since)
        return [p for p in self.points if p["timestamp"] > since]

    def engine_is_live(self):
        return True

    def engine_seen_at(self):
        return time.time()

    def ping(self):
        return True


def _callsigns(ws: _RecordingWS) -> list[str]:
    return [m["flights"][0]["callsign"] for m in ws.sent if m["type"] == "flights"]


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.02)


def test_connect_snapshot_never_lands_after_newer_push():
    async def _scenario() -> list[str]:
        store = _GatedStore()
        broadcaster = LiveBroadcaster(store, None)
        ws = _RecordingWS()
        store.block_next = True
        connect = asyncio.create_task(broadcaster.connect(ws))
        await _settle()  # the snapshot read holds the OLD state
        store.callsign = "NEW"
        tick = asyncio.create_task(broadcaster._broadcast_tick())
        await _settle()
        store.gate.set()
        await connect
        await tick
        await broadcaster._broadcast_tick()  # state unchanged since
        await _settle()
        return _callsigns(ws)

    assert asyncio.run(_scenario())[-1] == "NEW"


def test_subscribe_snapshot_never_lands_after_newer_push():
    async def _scenario() -> list[str]:
        store = _GatedStore()
        broadcaster = LiveBroadcaster(store, None)
        ws = _RecordingWS()
        await broadcaster.connect(ws, streams=["stats"])
        store.block_next = True
        broadcaster.set_streams(ws, ["stats", "flights"])
        await _settle()
        store.callsign = "NEW"
        tick = asyncio.create_task(broadcaster._broadcast_tick())
        await _settle()
        store.gate.set()
        await tick
        await _settle()
        await broadcaster._broadcast_tick()
        await _settle()
        return _callsigns(ws)

    assert asyncio.run(_scenario())[-1] == "NEW"


def _flight(integrity) -> dict:
    return {"flight_id": "f1", "timestamp": 1.0, "integrity": integrity}


def test_integrity_sig_sees_checks_emitter_and_rebroadcast():
    base = {"score": 0.4, "status": "suspect", "checks": {"tdoa": {"status": "warn"}}}
    variants = [
        {**base, "checks": {"tdoa": {"status": "fail"}}},
        {**base, "checks": {**base["checks"], "aoa": {"status": "pass"}}},
        {**base, "emitter": {"latitude": 35.7, "longitude": -78.7, "offset_km": 40.0}},
        {**base, "rebroadcast": True},
    ]
    for variant in variants:
        assert _flights_sig([_flight(base)]) != _flights_sig([_flight(variant)]), (
            variant
        )
    moved = {**variants[2], "emitter": {"latitude": 35.9, "longitude": -78.7}}
    assert _flights_sig([_flight(variants[2])]) != _flights_sig([_flight(moved)])
    # Detail text and sub-metre emitter jitter do not force a push.
    reworded = {**base, "checks": {"tdoa": {"status": "warn", "detail": "41.9 km"}}}
    assert _flights_sig([_flight(base)]) == _flights_sig([_flight(reworded)])
    jitter = {**variants[2], "emitter": {"latitude": 35.700001, "longitude": -78.7}}
    assert _flights_sig([_flight(variants[2])]) == _flights_sig([_flight(jitter)])


def test_integrity_sig_tolerates_null_score_and_odd_shapes():
    for integrity in (
        {"score": None, "status": "unknown", "checks": {}},
        {"score": None, "status": "unknown"},
        {"status": "ok", "checks": None, "emitter": None},
        {"status": "ok", "checks": {"x": "not-a-dict"}, "emitter": {"latitude": None}},
        None,
        "garbage",
    ):
        assert isinstance(_flights_sig([_flight(integrity)]), tuple)


def test_subscribing_to_telemetry_later_does_not_replay_a_backlog():
    async def _scenario() -> tuple[list[dict], list[float]]:
        store = _GatedStore()
        broadcaster = LiveBroadcaster(store, None)
        ws = _RecordingWS()
        await broadcaster.connect(ws, streams=["flights"])
        client = broadcaster._clients[ws]
        client.telemetry_floor -= 3600  # connected an hour ago
        client.telemetry_marks = {"old": client.telemetry_floor + 1}
        now = time.time()
        store.points = [
            {"flight_id": "f1", "timestamp": now - 1800},  # backlog
            {"flight_id": "f1", "timestamp": now + 5},  # arrives after subscribing
        ]
        broadcaster.set_streams(ws, ["flights", "telemetry"])
        assert client.telemetry_marks == {}
        await _settle()
        await broadcaster._broadcast_tick()
        await _settle()
        return [m for m in ws.sent if m["type"] == "telemetry"], store.telemetry_since

    sent, since_calls = asyncio.run(_scenario())
    # Only the point that arrived after subscribing, not the 30-minute backlog.
    assert len(sent) == 1
    assert len(sent[0]["telemetry"]) == 1
    assert sent[0]["telemetry"][0]["timestamp"] > time.time()
    assert since_calls and since_calls[-1] > time.time() - 60


def test_pings_survive_a_failing_data_tick():
    async def _scenario() -> list[str]:
        broadcaster = LiveBroadcaster(_GatedStore(), None)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("collect failed")

        broadcaster._collect_live_payload = _boom  # type: ignore[method-assign]
        client = _Client(telemetry_floor=time.time(), last_ping=0.0)
        broadcaster._clients = {object(): client}  # type: ignore[dict-item]
        await broadcaster.start()
        try:
            await _settle()
            return client.outbox.types()
        finally:
            await broadcaster.stop()

    assert "ping" in asyncio.run(_scenario())
