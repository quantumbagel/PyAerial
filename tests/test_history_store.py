from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time

import pytest
import redis

import pyaerial.store.history as history_mod
from fake_redis import FakeRedis
from helpers import (
    fine_track_plane,
    gnss_then_baro_plane,
    heading_plane,
    make_config,
    track_plane,
    with_unknown_then_ok,
)
from pyaerial.api import queries
from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.config.schema import ArchiveConfig
from pyaerial.constants import (
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.engine import Engine
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState, Verdict
from pyaerial.receivers.frames import RawFrame
from pyaerial.store.history import (
    HistoryStore,
    HistoryUnavailable,
    build_telemetry_docs,
)


def _plane(*, icao: str = "abc123", callsign: str = "SWA123"):
    return Plane(
        info=AircraftInfo(icao=icao, callsign=callsign, model="A320"),
        track=TrackState(first_packet=1.0, last_update=10.0),
        received_data={
            STORE_LAT: [Datum(35.725, 1.0), Datum(35.726, 10.0)],
            STORE_LONG: [Datum(-78.695, 1.0), Datum(-78.694, 10.0)],
        },
        calculated_data={
            STORE_HEADING: [Datum(45.0, 1.0)],
            STORE_HORIZ_SPEED: [Datum(200.0, 1.0)],
        },
    )


def _store(tmp_path, **kwargs) -> HistoryStore:
    # The archive policy is decided by the engine (Engine._retain); the store
    # writes what it is told.
    return HistoryStore(tmp_path / "pyaerial.db", **kwargs)


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
    shorter.received_data[STORE_LAT] = [Datum(35.726, 10.0)]
    shorter.received_data[STORE_LONG] = [Datum(-78.694, 10.0)]
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
    short.received_data[STORE_LAT] = [Datum(35.725, 1.0)]
    short.received_data[STORE_LONG] = [Datum(-78.695, 1.0)]
    assert store.finalize_plane(short, alerts=alerts) is True
    assert len(store.get_telemetry("abc123-1")) == 1
    longer = _plane()
    assert store.finalize_plane(longer, alerts=alerts) is True
    points = store.get_telemetry("abc123-1")
    assert len(points) == 2
    assert points[0]["latitude"] == 35.725
    store.close()


def test_snapshot_finalize_keeps_uninteresting_track(tmp_path):
    # The policy is the engine's: a crash snapshot with a position is kept
    # even when archive.flights: rules would drop it.
    engine = Engine(
        make_config(archive=ArchiveConfig(flights="rules")),
        isolated=True,
        aircraft_db_path=str(tmp_path / "aircraft.db"),
    )
    try:
        assert engine._retain(_plane(), [], from_snapshot=False) is False
        assert engine._retain(_plane(), [], from_snapshot=True) is True
    finally:
        engine.shutdown()
    store = _store(tmp_path)
    assert store.finalize_plane(_plane(), alerts=[], retained=True) is True
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
    store = _store(tmp_path)
    assert store.finalize_plane(_plane(), alerts=[], retained=False) is True
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
    plane.received_data[STORE_LAT] = [Datum(35.725, float(t)) for t in range(1, 11)]
    plane.received_data[STORE_LONG] = [
        Datum(-78.695, float(t)) for t in range(1, 11)
    ]
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
    plane.track.position_receivers = {1.0: ("nl", "be"), 10.0: ("nl",)}
    plane.track.integrity_log = [
        Verdict(
            0.5, 0.95, "ok", (), {"kinematics": {"status": "pass", "detail": "ok"}}
        ),
        Verdict(
            5.0,
            0.4,
            "spoofed",
            ("aoa", "tdoa"),
            {"aoa": {"status": "fail", "detail": "40°"}},
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
        store = HistoryStore(path)
    assert store.ping() is False
    assert "pyaerial reset" in caplog.text
    assert store.reset_all() is True
    assert store.ping() is True
    assert store.finalize_plane(_plane(), alerts=[]) is True
    store.close()


# --- Frame pruning in batches ------------------------------------------------


def test_prune_frames_deletes_backlog_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr("pyaerial.store.history.FRAME_PRUNE_BATCH", 3)
    store = HistoryStore(tmp_path / "pyaerial.db")
    store.write_frames(
        [
            RawFrame(hex="5d4ca2d4e3d2a7", timestamp=float(n), receiver="r")
            for n in range(7)
        ]
    )
    assert [store.prune_frames(100.0) for _ in range(4)] == [3, 3, 1, 0]
    store.close()


# --- Corrupt telemetry rows do not break reads -------------------------------


def test_corrupt_telemetry_json_is_dropped_not_raised(tmp_path, caplog):
    store = HistoryStore(tmp_path / "pyaerial.db")
    assert store.finalize_plane(heading_plane("abc123", 1.0, samples=2), alerts=[]) is True
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE telemetry SET receivers = '[\"nl\"', integrity_flags = 'oops' "
            "WHERE timestamp = 1.0"
        )
    conn.close()
    with caplog.at_level("WARNING", logger="pyaerial.store"):
        points = store.get_telemetry("abc123-1")
    assert len(points) == 2
    assert "receivers" not in points[0]
    assert "integrity_flags" not in points[0]
    assert points[0]["latitude"] == 35.0
    assert "abc123-1" in caplog.text
    store.close()


# --- gnss_height in archived telemetry ---------------------------------------


def test_telemetry_docs_carry_gnss_height_only_while_it_is_current():
    docs = build_telemetry_docs(gnss_then_baro_plane(), "abc123-1", "abc123")
    by_time = {doc["timestamp"]: doc for doc in docs}
    # Inside the compressed constant run, not just at its two datums.
    assert by_time[1.0]["gnss_height"] == 10972.8
    assert by_time[15.0]["gnss_height"] == 10972.8
    assert by_time[31.0]["gnss_height"] == 10972.8
    # Baro positions long after the last GNSS one get no stale GNSS height.
    assert "gnss_height" not in by_time[61.0]
    assert by_time[61.0]["altitude"] == 10890.0
    assert "gnss_height" not in build_telemetry_docs(track_plane(), "abc123-1", "abc123")[0]


def test_history_telemetry_api_returns_optional_gnss_height(tmp_path):
    store = HistoryStore(tmp_path / "pyaerial.db")
    assert store.finalize_plane(gnss_then_baro_plane(), alerts=[]) is True
    points = queries.get_telemetry(
        "abc123-1", "history", 0.0, live_store=None, history=store
    )
    assert points[0]["gnss_height"] == 10972.8
    assert "gnss_height" not in points[-1]
    assert points[-1]["altitude"] == 10890.0
    store.close()


# --- Verdicts without a score ------------------------------------------------


def test_scoreless_verdict_archives_with_the_flight(tmp_path):
    store = HistoryStore(tmp_path / "pyaerial.db")
    assert store.finalize_plane(with_unknown_then_ok(track_plane()), alerts=[]) is True
    points = store.get_telemetry("abc123-1")
    assert points[0]["integrity_status"] == "unknown"
    assert "integrity_score" not in points[0]
    assert points[-1]["integrity_score"] == 0.9
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT score, status FROM integrity ORDER BY timestamp"
        ).fetchall()
    conn.close()
    assert rows == [(None, "unknown"), (0.9, "ok")]
    store.close()


# --- Read-only portal archive ------------------------------------------------


def _hold_write_lock(path) -> sqlite3.Connection:
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO frames (timestamp, hex) VALUES (1, 'x')")
    return writer


def test_portal_opens_the_archive_without_waiting_for_the_engine(tmp_path):
    path = tmp_path / "pyaerial.db"
    writer_store = HistoryStore(path)
    assert writer_store.finalize_plane(fine_track_plane(), alerts=[]) is True
    writer_store.close()
    writer = _hold_write_lock(path)
    try:
        started = time.monotonic()
        portal = HistoryStore(path, read_only=True)
        assert portal.ping() is True
        assert [f["flight_id"] for f in portal.list_flights()] == ["abc123-1"]
        # The engine side too: a current schema needs no DDL, so no lock.
        engine_side = HistoryStore(path)
        assert engine_side.ping() is True
        assert time.monotonic() - started < 1.0
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    with pytest.raises(sqlite3.OperationalError):
        portal._conn.execute("CREATE TABLE x (y)")
    portal.close()
    engine_side.close()


def test_connect_stores_opens_history_read_only(tmp_path, monkeypatch):
    import pyaerial.api.connect as connect

    config = make_config()
    config.database.path = str(tmp_path / "pyaerial.db")
    monkeypatch.setattr(connect, "load_config", lambda _path: config)
    monkeypatch.setattr(
        redis.Redis, "from_url", staticmethod(lambda *a, **k: FakeRedis())
    )
    _config, history, live = connect.connect_stores("config.yaml")
    try:
        assert history.read_only is True
        assert connect.open_portal_history(config).read_only is True
    finally:
        history.close()
        live.close()


# --- History health: logs, heartbeat, newer schemas --------------------------


def test_unavailable_history_logs_the_error_rate_limited(tmp_path, caplog, monkeypatch):
    bad = tmp_path / "is_a_dir.db"
    bad.mkdir()
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store = HistoryStore(bad)
        store._last_connect_attempt = 0.0
        store.ping()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "unable to open" in errors[0].getMessage()
    assert store.health()["state"] == "down"
    assert "unable to open" in store.health()["error"]
    monkeypatch.setattr(history_mod, "_DOWN_LOG_INTERVAL", 0.0)
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store._last_connect_attempt = 0.0
        store.ping()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2
    store.close()


# --- Re-archive decisions ----------------------------------------------------


def test_clipped_snapshot_never_replaces_a_fuller_archive(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "pyaerial.db")
    full = fine_track_plane("abc123", 1_000.0, samples=100, step=10.0)  # 990 s
    assert store.finalize_plane(full, alerts=[]) is True
    # A crash-recovery snapshot: more points, but only the last 60 s.
    clipped = fine_track_plane("abc123", 1_000.0, samples=300, step=0.2)
    clipped.received_data[STORE_LAT] = [
        Datum(d.value, d.time + 930.0) for d in clipped.received_data[STORE_LAT]
    ]
    clipped.received_data[STORE_LONG] = [
        Datum(d.value, d.time + 930.0) for d in clipped.received_data[STORE_LONG]
    ]
    # Even if the old helper checks failed transiently, the decision is made
    # inside the write transaction from the archive itself.
    monkeypatch.setattr(
        store, "has_completed_flight", lambda _fid: False, raising=False
    )
    monkeypatch.setattr(store, "telemetry_count", lambda _fid: 0, raising=False)
    assert store.finalize_plane(clipped, alerts=[]) is True
    points = store.get_telemetry("abc123-1000")
    assert len(points) == 100 and points[0]["timestamp"] == 1_000.0
    # A longer series does upgrade it.
    longer = fine_track_plane("abc123", 1_000.0, samples=150, step=10.0)
    assert store.finalize_plane(longer, alerts=[]) is True
    assert len(store.get_telemetry("abc123-1000")) == 150
    store.close()


def test_rearchive_check_errors_mean_retry(tmp_path):
    store = HistoryStore(tmp_path / "pyaerial.db")
    assert (
        store.finalize_plane(fine_track_plane("abc123", 1_000.0, samples=50), alerts=[]) is True
    )
    with store._lock:
        store._conn.execute("PRAGMA busy_timeout=0")
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert (
            store.finalize_plane(fine_track_plane("abc123", 1_000.0, samples=5), alerts=[])
            is False
        )
    finally:
        blocker.rollback()
        blocker.close()
    assert len(store.get_telemetry("abc123-1000")) == 50
    store.close()
