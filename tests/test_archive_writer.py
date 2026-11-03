"""ArchiveWriter: finalize, re-archive decisions, raw frames, reset, health."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

import pyaerial.store.archive._conn as archive_conn
from helpers import (
    fine_track_plane,
    gnss_then_baro_plane,
    make_config,
    track_plane,
    with_unknown_then_ok,
)
from pyaerial.api.queries import get_telemetry
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
from pyaerial.store.archive import ArchiveReader, HistoryUnavailable
from pyaerial.store.archive.writer import ArchiveWriter
from pyaerial.store.codec import build_telemetry_docs


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


def _store(tmp_path, **kwargs) -> ArchiveWriter:
    # The archive policy is decided by the engine (FlightArchiver.retain); the store
    # writes what it is told.
    return ArchiveWriter(tmp_path / "pyaerial.db", **kwargs)


def test_finalize_writes_flight_telemetry_and_alerts(tmp_path):
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
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
    flights = archive.list_flights()
    assert len(flights) == 1
    assert flights[0]["icao"] == "abc123"
    assert flights[0]["callsign"] == "SWA123"
    points = archive.get_telemetry("abc123-1")
    assert len(points) == 2
    assert points[0]["latitude"] == 35.725
    assert points[0]["longitude"] == -78.695
    stored_alerts = archive.get_alerts()
    assert len(stored_alerts) == 1
    assert stored_alerts[0]["rule"] == "warn"
    assert stored_alerts[0]["latitude"] == 35.725
    assert stored_alerts[0]["longitude"] == -78.695
    archive.close()
    store.close()


def test_retention_is_the_engines_decision(tmp_path):
    # The policy is the engine's; the store writes what it is told.
    engine = Engine(
        make_config(archive=ArchiveConfig(flights="rules")),
        isolated=True,
        aircraft_db_path=str(tmp_path / "aircraft.db"),
    )
    try:
        assert engine.archiver.retain(_plane(), []) is False
    finally:
        engine.shutdown()
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
    assert store.finalize_plane(_plane(), alerts=[], retained=True) is True
    assert archive.count_flights() == 1
    archive.close()
    store.close()


def test_finalize_persists_alert_reason_dict_and_finite_eta(tmp_path):
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
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
    stored = archive.get_alerts()
    assert len(stored) == 1
    assert stored[0]["eta"] is None
    assert stored[0]["reason"]["rule"] == "warn"
    assert stored[0]["reason"]["zones"]["pad"] is None
    archive.close()
    store.close()


def test_unretained_flight_is_not_written(tmp_path):
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
    assert store.finalize_plane(_plane(), alerts=[], retained=False) is True
    assert archive.list_flights() == []
    archive.close()
    store.close()


def test_reset_and_delete_icao(tmp_path):
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
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
    icaos = {row[0] for row in sqlite3.connect(store.path).execute("SELECT icao FROM flights")}
    assert icaos == {"def456"}
    store.reset_all()
    assert archive.list_flights() == []
    assert archive.count_alerts() == 0
    archive.close()
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


def test_archive_all_writes_flights_without_alerts(tmp_path):
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
    assert store.finalize_plane(_plane(), alerts=[]) is True
    assert [doc["icao"] for doc in archive.list_flights()] == ["abc123"]
    archive.close()
    store.close()


def test_telemetry_carries_receivers_and_integrity_verdict(tmp_path):
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
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
    points = archive.get_telemetry("abc123-1")
    assert points[0]["receivers"] == ["nl", "be"]
    assert points[0]["integrity_status"] == "ok"
    assert points[0]["integrity_flags"] == []
    assert points[1]["integrity_status"] == "spoofed"
    assert points[1]["integrity_flags"] == ["aoa", "tdoa"]
    portal = get_telemetry("abc123-1", "history", 0.0, history=archive, live_store=None)
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
    archive.close()
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
        store = ArchiveWriter(path)
    assert store.ping() is False
    assert "pyaerial reset" in caplog.text
    assert store.reset_all() is True
    assert store.ping() is True
    assert store.finalize_plane(_plane(), alerts=[]) is True
    store.close()


# --- Frame pruning in batches ------------------------------------------------


def test_prune_frames_deletes_backlog_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr("pyaerial.store.archive.writer.FRAME_PRUNE_BATCH", 3)
    store = ArchiveWriter(tmp_path / "pyaerial.db")
    store.write_frames(
        [
            RawFrame(hex="5d4ca2d4e3d2a7", timestamp=float(n), receiver="r")
            for n in range(7)
        ]
    )
    assert [store.prune_frames(100.0) for _ in range(4)] == [3, 3, 1, 0]
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


# --- Verdicts without a score ------------------------------------------------


def test_scoreless_verdict_archives_with_the_flight(tmp_path):
    store = ArchiveWriter(tmp_path / "pyaerial.db")
    archive = ArchiveReader(store.path)
    assert store.finalize_plane(with_unknown_then_ok(track_plane()), alerts=[]) is True
    points = archive.get_telemetry("abc123-1")
    assert points[0]["integrity_status"] == "unknown"
    assert "integrity_score" not in points[0]
    assert points[-1]["integrity_score"] == 0.9
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT score, status FROM integrity ORDER BY timestamp"
        ).fetchall()
    conn.close()
    assert rows == [(None, "unknown"), (0.9, "ok")]
    archive.close()
    store.close()


# --- History health: logs, heartbeat, newer schemas --------------------------


def test_unavailable_history_logs_the_error_rate_limited(tmp_path, caplog, monkeypatch):
    bad = tmp_path / "is_a_dir.db"
    bad.mkdir()
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store = ArchiveWriter(bad)
        store._last_connect_attempt = 0.0
        store.ping()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "unable to open" in errors[0].getMessage()
    assert store.health()["state"] == "down"
    assert "unable to open" in store.health()["error"]
    monkeypatch.setattr(archive_conn, "_DOWN_LOG_INTERVAL", 0.0)
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store._last_connect_attempt = 0.0
        store.ping()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2
    store.close()


# --- Finalize replaces what checkpoints wrote; a locked archive means retry ---


def test_finalize_rewrites_the_whole_flight(tmp_path):
    store = ArchiveWriter(tmp_path / "pyaerial.db")
    archive = ArchiveReader(store.path)
    early = fine_track_plane("abc123", 1_000.0, samples=50)
    assert store.finalize_plane(early, alerts=[]) is True
    full = fine_track_plane("abc123", 1_000.0, samples=80)
    assert store.finalize_plane(full, alerts=[]) is True
    assert len(archive.get_telemetry("abc123-1000")) == 80
    archive.close()
    store.close()



def test_locked_archive_means_retry(tmp_path):
    store = ArchiveWriter(tmp_path / "pyaerial.db")
    archive = ArchiveReader(store.path)
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
    assert len(archive.get_telemetry("abc123-1000")) == 50
    archive.close()
    store.close()
