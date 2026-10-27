from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time

from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTEGRITY_LOG,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_POSITION_RECEIVERS,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum, Verdict
from pyaerial.receivers.frames import RawFrame
import pytest

from pyaerial.store.history import HistoryStore, HistoryUnavailable
from helpers import make_config, make_rule
from pyaerial.config.schema import ArchiveConfig, ZoneConfig


def _plane(*, icao: str = "abc123", callsign: str = "SWA123"):
    return {
        STORE_INFO: {STORE_ICAO: icao, STORE_CALLSIGN: callsign, "model": "A320"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.725, 1.0), Datum(35.726, 10.0)],
            STORE_LONG: [Datum(-78.695, 1.0), Datum(-78.694, 10.0)],
        },
        STORE_CALC_DATA: {
            STORE_HEADING: [Datum(45.0, 1.0)],
            STORE_HORIZ_SPEED: [Datum(200.0, 1.0)],
        },
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 1.0,
            STORE_MOST_RECENT_PACKET: 10.0,
        },
    }


def _store(tmp_path, *, archive: ArchiveConfig | None = None, **kwargs) -> HistoryStore:
    config = make_config(
        archive=archive,
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[make_rule(name="warn", retain=True, dwell_seconds=1)],
            )
        }
    )
    return HistoryStore(
        tmp_path / "pyaerial.db",
        config=config,
        polygons={},
        **kwargs,
    )


def test_finalize_writes_flight_telemetry_and_alerts(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "icao": "abc123",
            "callsign": "SWA123",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
            "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
            "altitude": 300.0,
        }
    ]
    assert store.finalize_plane(_plane(), alerts=alerts) is True
    flights = store.list_flights()
    assert len(flights) == 1
    assert flights[0]["icao"] == "abc123"
    assert flights[0]["callsign"] == "SWA123"
    points = store.get_telemetry("abc123-1")
    assert len(points) == 2
    assert points[0]["latitude"] == 35.725
    assert points[0]["longitude"] == -78.695
    stored_alerts = store.get_alerts()
    assert len(stored_alerts) == 1
    assert stored_alerts[0]["rule"] == "warn"
    assert stored_alerts[0]["latitude"] == 35.725
    assert stored_alerts[0]["longitude"] == -78.695
    store.close()


def test_finalize_does_not_replace_completed_telemetry(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 80.0,
        }
    ]
    plane = _plane()
    assert store.finalize_plane(plane, alerts=alerts) is True
    first = store.get_telemetry("abc123-1")
    assert len(first) == 2
    shorter = _plane()
    shorter[STORE_RECV_DATA][STORE_LAT] = [Datum(35.726, 10.0)]
    shorter[STORE_RECV_DATA][STORE_LONG] = [Datum(-78.694, 10.0)]
    assert store.finalize_plane(shorter, alerts=alerts) is True
    second = store.get_telemetry("abc123-1")
    assert len(second) == 2
    assert second[0]["latitude"] == 35.725
    store.close()


def test_finalize_upgrades_when_incoming_telemetry_is_longer(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 80.0,
        }
    ]
    short = _plane()
    short[STORE_RECV_DATA][STORE_LAT] = [Datum(35.725, 1.0)]
    short[STORE_RECV_DATA][STORE_LONG] = [Datum(-78.695, 1.0)]
    assert store.finalize_plane(short, alerts=alerts, from_snapshot=True) is True
    assert len(store.get_telemetry("abc123-1")) == 1
    longer = _plane()
    assert store.finalize_plane(longer, alerts=alerts) is True
    points = store.get_telemetry("abc123-1")
    assert len(points) == 2
    assert points[0]["latitude"] == 35.725
    store.close()


def test_snapshot_finalize_keeps_uninteresting_track(tmp_path):
    store = _store(tmp_path)
    plane = _plane()
    assert store.finalize_plane(plane, alerts=[], from_snapshot=True) is True
    assert store.count_flights() == 1
    store.close()


def test_finalize_persists_alert_reason_dict_and_finite_eta(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "icao": "abc123",
            "callsign": "SWA123",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
            "eta": float("inf"),
            "reason": {
                "zones": {"pad": float("inf")},
                "rule": "warn",
                "hook": "deactivate",
            },
            "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
        }
    ]
    assert store.finalize_plane(_plane(), alerts=alerts) is True
    stored = store.get_alerts()
    assert len(stored) == 1
    assert stored[0]["eta"] is None
    assert stored[0]["reason"]["rule"] == "warn"
    assert stored[0]["reason"]["zones"]["pad"] is None
    store.close()


def test_unretained_flight_is_not_written(tmp_path):
    store = _store(tmp_path, archive=ArchiveConfig(flights="rules"))
    assert store.finalize_plane(_plane(), alerts=[]) is True
    assert store.list_flights() == []
    store.close()


def test_search_and_range_filters(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
        }
    ]
    store.finalize_plane(_plane(), alerts=alerts)
    store.finalize_plane(_plane(icao="def456", callsign="DAL99"), alerts=alerts)
    found = store.list_flights(q="swa")
    assert [doc["icao"] for doc in found] == ["abc123"]
    starred = store.list_flights(q="SWA*")
    assert starred == []
    ranged = store.list_flights(since=5.0, until=15.0)
    assert len(ranged) == 2
    empty = store.list_flights(until=0.5)
    assert empty == []
    store.close()


def test_portal_history_queries_use_sqlite(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "icao": "abc123",
            "callsign": "SWA123",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
            "latitude": 35.725,
            "longitude": -78.695,
        }
    ]
    store.finalize_plane(_plane(), alerts=alerts)
    summaries = get_history_flights(store, None)
    assert len(summaries) == 1
    assert summaries[0]["flight_id"] == "abc123-1"
    assert summaries[0]["alert_stats"]["episode_count"] == 1
    detail = get_flight_detail(
        "abc123-1", "history", live_store=None, history=store, aircraft_db=None
    )
    assert detail is not None
    assert detail["model"] == "A320"
    tel = get_telemetry("abc123-1", "history", 0.0, live_store=None, history=store)
    assert len(tel) == 2
    hist_alerts = get_alerts("history", live_store=None, history=store, q="warn")
    assert len(hist_alerts) == 1
    stats = get_stats(None, store)
    assert stats["history"] is True
    assert stats["retained_flights"] == 1
    assert stats["historical_alerts"] == 1
    store.close()


def test_reset_and_delete_icao(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
        }
    ]
    store.finalize_plane(_plane(), alerts=alerts)
    store.finalize_plane(_plane(icao="def456", callsign="DAL99"), alerts=alerts)
    store.delete_icao("abc123")
    assert store.has_icao("abc123") is False
    assert store.has_icao("def456") is True
    store.reset_all()
    assert store.list_flights() == []
    assert store.count_alerts() == 0
    store.close()


def test_list_flights_errors_when_disconnected(tmp_path):
    store = _store(tmp_path, disabled=True)
    with pytest.raises(HistoryUnavailable):
        store.list_flights()
    with pytest.raises(HistoryUnavailable):
        store.get_alerts()
    with pytest.raises(HistoryUnavailable):
        store.get_telemetry("abc123-1")
    store.close()


def test_get_telemetry_limit_pages_forward(tmp_path):
    store = _store(tmp_path)
    plane = _plane()
    plane[STORE_RECV_DATA][STORE_LAT] = [Datum(35.725, float(t)) for t in range(1, 11)]
    plane[STORE_RECV_DATA][STORE_LONG] = [Datum(-78.695, float(t)) for t in range(1, 11)]
    alerts = [{"zone": "pad", "rule": "warn", "activated_at": 1.0}]
    assert store.finalize_plane(plane, alerts=alerts)
    fid = "abc123-1"
    assert len(store.get_telemetry(fid)) == 10
    limited = store.get_telemetry(fid, limit=3)
    assert [p["timestamp"] for p in limited] == [1.0, 2.0, 3.0]
    page = store.get_telemetry(fid, since=limited[-1]["timestamp"], limit=3)
    assert [p["timestamp"] for p in page] == [4.0, 5.0, 6.0]
    assert [p["timestamp"] for p in store.get_telemetry(fid, since=2.0, limit=0)] == [
        float(t) for t in range(3, 11)
    ]
    newest = store.get_telemetry(fid, limit=3, newest=True)
    assert [p["timestamp"] for p in newest] == [8.0, 9.0, 10.0]
    store.close()


def test_reset_and_delete_raise_cleanly_when_database_locked(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    store.finalize_plane(
        _plane(), alerts=[{"zone": "pad", "rule": "warn", "activated_at": 1.0}]
    )
    with store._lock:
        assert store._conn is not None
        store._conn.execute("PRAGMA busy_timeout=0")
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(HistoryUnavailable, match="locked"):
            store.delete_icao("abc123")
        with pytest.raises(HistoryUnavailable, match="locked"):
            store.reset_all()
    finally:
        blocker.rollback()
        blocker.close()
    assert store.delete_icao("abc123") is True
    store.close()


def test_queries_survive_close_between_check_and_use(tmp_path):
    """Regression: readers checked _conn, released the lock, then asserted it,
    so a close() from another thread in between raised AssertionError."""
    store = _store(tmp_path)
    alerts = [{"zone": "pad", "rule": "warn", "activated_at": 1.0}]
    store.finalize_plane(_plane(), alerts=alerts)
    original = store._ensure_connected

    def close_after_check() -> bool:
        ok = original()
        store.close()  # another thread wins the race here
        return ok

    store._ensure_connected = close_after_check  # type: ignore[method-assign]
    for call in (
        store.list_flights,
        lambda: store.get_flight("abc123-1"),
        lambda: store.get_telemetry("abc123-1"),
        store.get_alerts,
        store.count_flights,
        store.distinct_icaos,
    ):
        store._last_connect_attempt = time.monotonic()  # no instant reconnect
        with contextlib.suppress(HistoryUnavailable):
            call()
    store._last_connect_attempt = 0.0
    assert store.finalize_plane(_plane(), alerts=alerts) is True
    store.close()


def test_archive_all_writes_flights_without_alerts(tmp_path):
    store = _store(tmp_path)
    assert store.finalize_plane(_plane(), alerts=[]) is True
    assert [doc["icao"] for doc in store.list_flights()] == ["abc123"]
    store.close()


def test_telemetry_carries_receivers_and_integrity_verdict(tmp_path):
    store = _store(tmp_path)
    plane = _plane()
    plane[STORE_INTERNAL][STORE_POSITION_RECEIVERS] = {1.0: ("nl", "be"), 10.0: ("nl",)}
    plane[STORE_INTERNAL][STORE_INTEGRITY_LOG] = [
        Verdict(0.5, 0.95, "ok", (), {"kinematics": {"status": "pass", "detail": "ok"}}),
        Verdict(
            5.0, 0.4, "spoofed", ("aoa", "tdoa"), {"aoa": {"status": "fail", "detail": "40°"}}
        ),
    ]
    assert store.finalize_plane(plane, alerts=[]) is True
    points = store.get_telemetry("abc123-1")
    assert points[0]["receivers"] == ["nl", "be"]
    assert points[0]["integrity_status"] == "ok"
    assert points[0]["integrity_flags"] == []
    assert points[1]["integrity_status"] == "spoofed"
    assert points[1]["integrity_flags"] == ["aoa", "tdoa"]
    portal = get_telemetry("abc123-1", "history", 0.0, history=store, live_store=None)
    assert portal[1]["integrity_score"] == 0.4

    with sqlite3.connect(store.path) as conn:
        verdicts = conn.execute(
            "SELECT timestamp, status, flags, checks FROM integrity ORDER BY timestamp"
        ).fetchall()
        multi = conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE json_array_length(receivers) >= 2"
        ).fetchone()[0]
    assert [(row[0], row[1]) for row in verdicts] == [(0.5, "ok"), (5.0, "spoofed")]
    assert json.loads(verdicts[1][2]) == ["aoa", "tdoa"]
    assert json.loads(verdicts[1][3])["aoa"]["status"] == "fail"
    assert multi == 1
    store.close()


def test_raw_frames_are_archived_pruned_and_deleted_by_icao(tmp_path):
    store = _store(tmp_path)
    frames = [
        RawFrame(
            hex="8D406B902015A678D4D220AA4BDA",
            timestamp=100.0,
            receiver="nl",
            rssi=-12.0,
            clock=5,
        ),
        RawFrame(
            hex="8d406b902015a678d4d220aa4bda",
            timestamp=100.0,
            receiver="be",
            rssi=float("-inf"),
        ),
        RawFrame(hex="5d4ca2d4e3d2a7", timestamp=200.0, receiver="nl"),
    ]
    assert store.write_frames(frames) is True

    def rows():
        with sqlite3.connect(store.path) as conn:
            return conn.execute(
                "SELECT timestamp, receiver, hex, df, icao, rssi, clock FROM frames "
                "ORDER BY timestamp, receiver"
            ).fetchall()

    assert rows() == [
        (100.0, "be", "8d406b902015a678d4d220aa4bda", 17, "406b90", None, None),
        (100.0, "nl", "8d406b902015a678d4d220aa4bda", 17, "406b90", -12.0, 5),
        (200.0, "nl", "5d4ca2d4e3d2a7", 11, None, None, None),
    ]
    assert store.prune_frames(150.0) == 2
    assert len(rows()) == 1
    store.write_frames(frames[:1])
    assert store.delete_icao("406B90") is True
    assert [row[2] for row in rows()] == ["5d4ca2d4e3d2a7"]
    store.close()


def test_old_schema_refuses_to_open_until_reset(tmp_path, caplog):
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE flights (flight_id TEXT)")
        conn.execute("PRAGMA user_version = 1")
    conn.close()
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store = HistoryStore(path, config=make_config(), polygons={})
    assert store.ping() is False
    assert "pyaerial reset" in caplog.text
    assert store.reset_all() is True
    assert store.ping() is True
    assert store.finalize_plane(_plane(), alerts=[]) is True
    store.close()
