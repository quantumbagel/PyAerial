"""ArchiveReader: the portal's read-only archive (queries, ``mode=ro``, state)."""

from __future__ import annotations

import contextlib
import sqlite3
import time

import pytest
import redis

from fake_redis import FakeRedis
from helpers import (
    fine_track_plane,
    gnss_then_baro_plane,
    heading_plane,
    make_config,
)
from pyaerial.api import queries
from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.constants import (
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState
from pyaerial.store.archive import ArchiveReader, ArchiveWriter, HistoryUnavailable


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
    # The archive policy is decided by the engine (Engine._retain); the store
    # writes what it is told.
    return ArchiveWriter(tmp_path / "pyaerial.db", **kwargs)


def test_search_and_range_filters(tmp_path):
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
    found = archive.list_flights(q="swa")
    assert [doc["icao"] for doc in found] == ["abc123"]
    starred = archive.list_flights(q="SWA*")
    assert starred == []
    ranged = archive.list_flights(since=5.0, until=15.0)
    assert len(ranged) == 2
    empty = archive.list_flights(until=0.5)
    assert empty == []
    archive.close()
    store.close()


def test_portal_history_queries_use_sqlite(tmp_path):
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
            "latitude": 35.725,
            "longitude": -78.695,
        }
    ]
    store.finalize_plane(_plane(), alerts=alerts)
    summaries = get_history_flights(archive, None)
    assert len(summaries) == 1
    assert summaries[0]["flight_id"] == "abc123-1"
    assert summaries[0]["alert_stats"]["episode_count"] == 1
    detail = get_flight_detail(
        "abc123-1", "history", live_store=None, history=archive, aircraft_db=None
    )
    assert detail is not None
    assert detail["model"] == "A320"
    tel = get_telemetry("abc123-1", "history", 0.0, live_store=None, history=archive)
    assert len(tel) == 2
    hist_alerts = get_alerts("history", live_store=None, history=archive, q="warn")
    assert len(hist_alerts) == 1
    stats = get_stats(None, archive)
    assert stats["history"] is True
    assert stats["retained_flights"] == 1
    assert stats["historical_alerts"] == 1
    archive.close()
    store.close()


def test_list_flights_errors_when_disconnected(tmp_path):
    # No archive file yet: the reader has no connection.
    archive = ArchiveReader(tmp_path / "pyaerial.db")
    with pytest.raises(HistoryUnavailable):
        archive.list_flights()
    with pytest.raises(HistoryUnavailable):
        archive.get_alerts()
    with pytest.raises(HistoryUnavailable):
        archive.get_telemetry("abc123-1")
    archive.close()


def test_get_telemetry_limit_pages_forward(tmp_path):
    store = _store(tmp_path)
    archive = ArchiveReader(store.path)
    plane = _plane()
    plane.received_data[STORE_LAT] = [Datum(35.725, float(t)) for t in range(1, 11)]
    plane.received_data[STORE_LONG] = [
        Datum(-78.695, float(t)) for t in range(1, 11)
    ]
    alerts = [{"zone": "pad", "rule": "warn", "activated_at": 1.0}]
    assert store.finalize_plane(plane, alerts=alerts)
    fid = "abc123-1"
    assert len(archive.get_telemetry(fid)) == 10
    limited = archive.get_telemetry(fid, limit=3)
    assert [p["timestamp"] for p in limited] == [1.0, 2.0, 3.0]
    page = archive.get_telemetry(fid, since=limited[-1]["timestamp"], limit=3)
    assert [p["timestamp"] for p in page] == [4.0, 5.0, 6.0]
    assert [p["timestamp"] for p in archive.get_telemetry(fid, since=2.0, limit=0)] == [
        float(t) for t in range(3, 11)
    ]
    newest = archive.get_telemetry(fid, limit=3, newest=True)
    assert [p["timestamp"] for p in newest] == [8.0, 9.0, 10.0]
    archive.close()
    store.close()


def test_queries_survive_close_between_check_and_use(tmp_path):
    """Regression: readers checked _conn, released the lock, then asserted it,
    so a close() from another thread in between raised AssertionError."""
    store = _store(tmp_path)
    alerts = [{"zone": "pad", "rule": "warn", "activated_at": 1.0}]
    store.finalize_plane(_plane(), alerts=alerts)
    archive = ArchiveReader(store.path)
    for call in (
        archive.list_flights,
        lambda: archive.get_flight("abc123-1"),
        lambda: archive.get_telemetry("abc123-1"),
        archive.get_alerts,
        archive.count_flights,
        archive.distinct_icaos,
    ):
        archive.close()  # another thread wins the race here
        archive._last_connect_attempt = time.monotonic()  # no instant reconnect
        with contextlib.suppress(HistoryUnavailable):
            call()
    archive._last_connect_attempt = 0.0
    assert [doc["flight_id"] for doc in archive.list_flights()] == ["abc123-1"]
    # The writer's check-then-write path.
    original = store._ensure_connected

    def close_after_check() -> bool:
        ok = original()
        store.close()  # another thread wins the race here
        return ok

    store._ensure_connected = close_after_check  # type: ignore[method-assign]
    assert store.finalize_plane(_plane(), alerts=alerts) is True
    archive.close()
    store.close()


# --- Corrupt telemetry rows do not break reads -------------------------------


def test_corrupt_telemetry_json_is_dropped_not_raised(tmp_path, caplog):
    store = ArchiveWriter(tmp_path / "pyaerial.db")
    archive = ArchiveReader(store.path)
    assert store.finalize_plane(heading_plane("abc123", 1.0, samples=2), alerts=[]) is True
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE telemetry SET receivers = '[\"nl\"', integrity_flags = 'oops' "
            "WHERE timestamp = 1.0"
        )
    conn.close()
    with caplog.at_level("WARNING", logger="pyaerial.store"):
        points = archive.get_telemetry("abc123-1")
    assert len(points) == 2
    assert "receivers" not in points[0]
    assert "integrity_flags" not in points[0]
    assert points[0]["latitude"] == 35.0
    assert "abc123-1" in caplog.text
    archive.close()
    store.close()


def test_history_telemetry_api_returns_optional_gnss_height(tmp_path):
    store = ArchiveWriter(tmp_path / "pyaerial.db")
    archive = ArchiveReader(store.path)
    assert store.finalize_plane(gnss_then_baro_plane(), alerts=[]) is True
    points = queries.get_telemetry(
        "abc123-1", "history", 0.0, live_store=None, history=archive
    )
    assert points[0]["gnss_height"] == 10972.8
    assert "gnss_height" not in points[-1]
    assert points[-1]["altitude"] == 10890.0
    archive.close()
    store.close()


# --- Read-only portal archive ------------------------------------------------


def _hold_write_lock(path) -> sqlite3.Connection:
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO frames (timestamp, hex) VALUES (1, 'x')")
    return writer


def test_portal_opens_the_archive_without_waiting_for_the_engine(tmp_path):
    path = tmp_path / "pyaerial.db"
    writer_store = ArchiveWriter(path)
    assert writer_store.finalize_plane(fine_track_plane(), alerts=[]) is True
    writer_store.close()
    writer = _hold_write_lock(path)
    try:
        started = time.monotonic()
        portal = ArchiveReader(path)
        assert portal.ping() is True
        assert [f["flight_id"] for f in portal.list_flights()] == ["abc123-1"]
        # The engine side too: a current schema needs no DDL, so no lock.
        engine_side = ArchiveWriter(path)
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
        assert isinstance(history, ArchiveReader) and history.read_only is True
        portal = connect.open_portal_history(config)
        assert isinstance(portal, ArchiveReader) and portal.read_only is True
        portal.close()
    finally:
        history.close()
        live.close()
