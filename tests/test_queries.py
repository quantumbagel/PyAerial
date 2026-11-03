from __future__ import annotations

import time

import pytest

from pyaerial.api import queries
from pyaerial.api.queries import (
    get_alerts,
    get_history_flights,
    get_live_flights,
    get_stats,
    get_telemetry,
)
from helpers import LOCAL_REDIS_URI
from pyaerial.store.archive import HistoryUnavailable
from pyaerial.store.live import LiveReader, LiveUnavailable, LiveWriter


def test_get_stats_reports_store_health_and_engine_heartbeat(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    stats = get_stats(store, None)
    assert stats["live_flights"] == 0
    assert stats["redis"] is True
    assert stats["history"] is False
    assert stats["engine_seen_at"] is None
    store.touch_engine()
    stats = get_stats(store, None)
    assert isinstance(stats["engine_seen_at"], float)
    store.clear_engine()
    assert get_stats(store, None)["engine_seen_at"] is None


def test_claim_engine_fails_when_redis_down(fake):
    fake.down = True
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine() is False


def test_live_queries_hide_flights_when_engine_heartbeat_is_stale(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    from pyaerial.constants import STORE_LAT, STORE_LONG
    from pyaerial.models import AircraftInfo, Datum, Plane, TrackState

    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=1.0, last_update=1.0),
        received_data={
            STORE_LAT: [Datum(35.72, 1.0)],
            STORE_LONG: [Datum(-78.70, 1.0)],
        },
    )
    store.write_live_planes({"abc123": plane})
    store.clear_engine()
    assert get_live_flights(store, None) == []
    store.touch_engine()
    flights = get_live_flights(store, None)
    assert len(flights) == 1
    assert flights[0]["icao"] == "abc123"


def test_claim_engine_succeeds_without_a_rival(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine() is True
    assert store.ensure_writer() is True


def test_claim_engine_nx_refuses_foreign_token(fake):
    import json

    from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS

    first = LiveWriter(LOCAL_REDIS_URI)
    assert first.claim_engine() is True
    second = LiveWriter(LOCAL_REDIS_URI)
    assert second.claim_engine() is False
    raw = json.loads(fake.get("live:engine"))
    assert raw["token"] == first._engine_token
    assert time.time() - raw["seen_at"] < LIVE_ENGINE_TTL_SECONDS


def test_touch_engine_does_not_overwrite_foreign_heartbeat(fake):
    first = LiveWriter(LOCAL_REDIS_URI)
    assert first.claim_engine() is True
    owned = fake.get("live:engine")
    LiveWriter(LOCAL_REDIS_URI).touch_engine()
    assert fake.get("live:engine") == owned


def test_ensure_writer_yields_to_foreign_token(fake):
    first = LiveWriter(LOCAL_REDIS_URI)
    assert first.claim_engine() is True
    second = LiveWriter(LOCAL_REDIS_URI)
    assert second.ensure_writer() is False
    assert second.writer is False
    assert first.ensure_writer() is True
    assert first.writer is True


def test_clear_engine_does_not_delete_foreign_heartbeat(fake):
    first = LiveWriter(LOCAL_REDIS_URI)
    assert first.claim_engine() is True
    owned = fake.get("live:engine")
    LiveWriter(LOCAL_REDIS_URI).clear_engine()
    assert fake.get("live:engine") == owned


def test_write_live_planes_skips_redis_after_yield(fake):
    first = LiveWriter(LOCAL_REDIS_URI)
    assert first.claim_engine() is True
    second = LiveWriter(LOCAL_REDIS_URI)
    assert second.ensure_writer() is False
    fake.calls.clear()
    second.write_live_planes({"x": {}})
    assert "execute" not in fake.calls
    assert fake.get("live:engine") is not None


def test_history_queries_error_when_archive_down():
    class Down:
        def ping(self):
            return False

    down = Down()
    with pytest.raises(HistoryUnavailable):
        get_history_flights(down, None)
    with pytest.raises(HistoryUnavailable):
        get_alerts("history", live_store=None, history=down)
    with pytest.raises(HistoryUnavailable):
        get_telemetry("x", "history", 0.0, live_store=None, history=down)


@pytest.mark.parametrize("role", ["portal", "maintenance"])
def test_reader_get_flights_errors_when_redis_down(fake, role):
    fake.down = True
    if role == "portal":
        store = LiveReader(LOCAL_REDIS_URI)
    else:  # pyaerial reset's handle
        store = LiveWriter(LOCAL_REDIS_URI, writer=False)
    store._last_connect_attempt = time.monotonic()
    with pytest.raises(LiveUnavailable):
        store.get_flights()
    assert get_live_flights(store, None) == []
    stats = get_stats(store, None)
    assert stats["redis"] is False
    assert stats["live_flights"] == 0


def test_maintenance_pop_flight_deletes_redis(fake):
    from helpers import track_plane

    engine = LiveWriter(LOCAL_REDIS_URI)
    engine.write_live_planes({"ABC123": track_plane("abc123", 1.0)})
    store = LiveWriter(LOCAL_REDIS_URI, writer=False)
    store.pop_flight("abc123-1")
    assert not fake.sets.get("live:flights")
    assert "live:flight:abc123-1" not in fake.kv


def test_get_stats_without_store():
    stats = get_stats(None, None)
    assert stats["redis"] is False
    assert stats["history"] is False
    assert stats["engine_seen_at"] is None


# --- Live query cost and size bounds -----------------------------------------


class _AlertStore:
    def __init__(self, alerts):
        self.alerts = alerts

    def engine_is_live(self):
        return True

    def get_alerts(self, **_kwargs):
        return [dict(alert) for alert in self.alerts]


def _alert(i: int, *, active: bool) -> dict:
    return {
        "alert_id": f"a{i}",
        "flight_id": "f1",
        "active": active,
        "activated_at": float(i),
        "deactivated_at": None if active else float(i) + 1,
    }


def test_tracked_alerts_keep_active_first_then_newest():
    store = _AlertStore([_alert(i, active=i in (1, 3)) for i in range(6)])
    out = queries.get_tracked_live_alerts(store, [{"flight_id": "f1"}], limit=3)
    assert [a["alert_id"] for a in out] == ["a3", "a1", "a5"]


def test_tracked_alerts_scale_linearly():
    """Partitioning used a list-of-dicts membership test: O(n^2) every tick."""
    store = _AlertStore([_alert(i, active=i % 2 == 0) for i in range(20_000)])
    started = time.perf_counter()
    out = queries.get_tracked_live_alerts(store, [{"flight_id": "f1"}], limit=50)
    assert time.perf_counter() - started < 1.0
    assert len(out) == 10_000
    assert all(alert["active"] for alert in out)


class _History:
    """Mimics ArchiveReader.get_telemetry paging semantics."""

    def __init__(self, count: int):
        self.count = count

    def ping(self):
        return True

    def get_telemetry(self, flight_id, *, since=0.0, limit=None, newest=False):
        points = [
            {"timestamp": float(t), "latitude": 1.0, "longitude": 2.0}
            for t in range(self.count)
            if since <= 0 or t > since
        ]
        if limit:
            points = points[-limit:] if newest else points[:limit]
        return points


class _Live(_AlertStore):
    def __init__(self, count: int):
        super().__init__([])
        self.count = count

    def get_telemetry(self, flight_id, *, since=0.0, limit=None, newest=False):
        points = [
            {"timestamp": float(t), "latitude": 1.0, "longitude": 2.0}
            for t in range(self.count)
            if since <= 0 or t > since
        ]
        if limit:
            points = points[-limit:] if newest else points[:limit]
        return points


def _telemetry(view, count, since=0.0, limit=None):
    store = _Live(count) if view == "live" else _History(count)
    return queries.get_telemetry(
        "f",
        view,
        since,
        live_store=store if view == "live" else None,
        history=store if view == "history" else None,
        limit=limit,
    )


def test_telemetry_cap_applies_to_live_and_history():
    cap = queries.MAX_TELEMETRY_POINTS
    for view in ("live", "history"):
        points = _telemetry(view, cap + 7)
        assert len(points) == cap, view
        assert points.truncated is True, view
        # No cursor: newest points, so the path ends where the flight is.
        assert points[-1]["timestamp"] == float(cap + 6), view
        # With a cursor: page forward from it.
        paged = _telemetry(view, 100, since=9.0, limit=5)
        assert [p["timestamp"] for p in paged] == [10.0, 11.0, 12.0, 13.0, 14.0]
        assert paged.truncated is True
        whole = _telemetry(view, 5, limit=5)
        assert len(whole) == 5 and whole.truncated is False, view


def test_ws_fetch_telemetry_reports_truncation(fake):
    from fastapi.testclient import TestClient

    from helpers import make_config, track_plane
    from pyaerial.webapp import create_app

    store = LiveWriter(LOCAL_REDIS_URI, telemetry_keep_seconds=0)
    store.touch_engine()
    total = queries.DEFAULT_TELEMETRY_POINTS + 10
    plane = track_plane("abc123", 0.0, samples=total, lat_step=1e-6)
    store.write_live_planes({"ABC123": plane})
    app = create_app(config=make_config(), live_store=store)
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/live?streams=stats") as ws,
    ):
        for params, size, truncated in (
            ({}, queries.DEFAULT_TELEMETRY_POINTS, True),
            ({"limit": 10**9}, total, False),
        ):
            ws.send_json(
                {
                    "type": "request",
                    "id": "t",
                    "action": "fetchTelemetry",
                    "params": {"flightId": "abc123-0", "view": "live", **params},
                }
            )
            while (reply := ws.receive_json()).get("type") != "response":
                pass
            assert reply["success"] is True
            assert len(reply["data"]) == size
            assert reply["truncated"] is truncated
            assert reply["data"][-1]["timestamp"] == float(total - 1)
