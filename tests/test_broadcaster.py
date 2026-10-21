"""LiveBroadcaster outbox, telemetry cursor and threading regressions."""

from __future__ import annotations

import asyncio
import time

from pyaerial.api.broadcaster import LiveBroadcaster, _Client, _Outbox
from pyaerial.api.queries import get_tracked_live_alerts
from pyaerial.api.spec import ALERTS_PUSH_MIN


def test_outbox_never_evicts_responses():
    async def _scenario() -> None:
        box = _Outbox(maxsize=3)
        assert box.put({"type": "response", "id": 1})
        assert box.put({"type": "flights"})
        assert box.put({"type": "response", "id": 2})
        # Full: a new snapshot evicts the oldest non-response message.
        assert box.put({"type": "alerts"})
        assert box.types() == ["response", "response", "alerts"]
        # Low-value streams are skipped rather than evicting anything.
        assert not box.put({"type": "telemetry"})
        # A response may evict a stream snapshot...
        assert box.put({"type": "response", "id": 3})
        assert box.types() == ["response", "response", "response"]
        # ...but never another response: the client is stuck.
        assert not box.put({"type": "response", "id": 4})
        assert not box.put({"type": "flights"})
        assert (await box.get())["id"] == 1

    asyncio.run(_scenario())


def test_outbox_get_waits_for_put():
    async def _scenario() -> dict:
        box = _Outbox(maxsize=2)
        getter = asyncio.create_task(box.get())
        await asyncio.sleep(0)
        box.put({"type": "ping"})
        return await asyncio.wait_for(getter, timeout=1)

    assert asyncio.run(_scenario())["type"] == "ping"


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
        return [item for item in client.outbox._items if item["type"] == "telemetry"]

    sent = asyncio.run(_scenario())
    assert len(sent) == 1
    assert sent[0]["telemetry"] == [_point("f1", 250.0)]


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
