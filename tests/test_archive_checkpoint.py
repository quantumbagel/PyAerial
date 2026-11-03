"""Incremental archive writes (``ArchiveWriter.checkpoint``, ``upsert_episodes``,
``discard_flight``) and the codec slices they are built from."""

from __future__ import annotations

import sqlite3

from helpers import track_plane, with_unknown_then_ok
from pyaerial.constants import STORE_LAT
from pyaerial.store.archive import ArchiveReader, ArchiveWriter
from pyaerial.store.archive.rows import flight_from_row, telemetry_from_row
from pyaerial.store.archive.writer import FlightCheckpoint
from pyaerial.store.codec import (
    build_telemetry_docs,
    flight_row,
    integrity_rows,
    plane_from_rows,
)


def _rows(path, sql, *args):
    with sqlite3.connect(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def _slice(plane, *, since=None, until=None, after=None) -> FlightCheckpoint:
    fid, icao = plane.flight_id, plane.icao
    return FlightCheckpoint(
        plane,
        build_telemetry_docs(plane, fid, icao, since=since, until=until),
        integrity_rows(plane, fid, icao, after=after),
    )


def _episode(plane, alert_id, *, active, at, last=None, **extra):
    doc = {
        "alert_id": alert_id,
        "flight_id": plane.flight_id,
        "icao": plane.icao,
        "zone": "pad",
        "rule": "warn",
        "active": active,
        "activated_at": at,
        "deactivated_at": None if active else (last or at),
        "last_updated": last or at,
        "position": {"type": "Point", "coordinates": [-78.0, 35.0]},
        **extra,
    }
    return (flight_row(plane), doc)


def test_codec_slices_select_samples_and_verdicts():
    plane = with_unknown_then_ok(track_plane(first=100.0, samples=5))
    fid, icao = plane.flight_id, plane.icao
    stamps = [d["timestamp"] for d in build_telemetry_docs(plane, fid, icao)]
    assert stamps == [100.0, 101.0, 102.0, 103.0, 104.0]
    sliced = build_telemetry_docs(plane, fid, icao, since=100.0, until=102.0)
    assert [d["timestamp"] for d in sliced] == [101.0, 102.0]
    assert [r[1] for r in integrity_rows(plane, fid, icao, after=100.0)] == [101.5]


def test_checkpoint_writes_a_hidden_live_row_then_finalize_completes_it(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    plane = with_unknown_then_ok(track_plane(first=100.0, samples=5))
    assert store.checkpoint([_slice(plane, until=102.0)], now=500.0)
    assert _rows(path, "SELECT status, checkpoint_at, end_time FROM flights") == [
        ("live", 500.0, 104.0)
    ]
    assert _rows(path, "SELECT COUNT(*) FROM telemetry") == [(3,)]
    assert _rows(path, "SELECT COUNT(*) FROM integrity") == [(2,)]
    reader = ArchiveReader(path)
    assert reader.list_flights() == [] and reader.count_flights() == 0

    # The next slice adds the rest; nothing is duplicated.
    assert store.checkpoint([_slice(plane, since=102.0, after=101.5)], now=505.0)
    assert _rows(path, "SELECT COUNT(*) FROM telemetry") == [(5,)]
    assert _rows(path, "SELECT checkpoint_at FROM flights") == [(505.0,)]

    assert store.finalize_plane(plane)
    assert _rows(path, "SELECT status, checkpoint_at FROM flights") == [
        ("completed", 505.0)
    ]
    assert _rows(path, "SELECT COUNT(*) FROM telemetry") == [(5,)]
    assert [f["flight_id"] for f in reader.list_flights()] == [plane.flight_id]
    reader.close()
    store.close()


def test_checkpoint_never_reopens_a_completed_flight(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    plane = track_plane(first=100.0, samples=3)
    assert store.finalize_plane(plane)
    assert store.checkpoint([_slice(plane)], now=500.0)
    assert _rows(path, "SELECT status FROM flights") == [("completed",)]
    store.close()


def test_checkpoint_reports_an_unavailable_archive(tmp_path):
    store = ArchiveWriter(tmp_path / "a.db")
    store.close()
    store._conn = None
    store._last_connect_attempt = float("inf")
    assert not store.checkpoint([_slice(track_plane())], now=1.0)


def test_episode_creates_its_flight_row_and_keeps_its_history(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    plane = track_plane(first=100.0, samples=3)
    fid = plane.flight_id
    assert store.upsert_episodes([_episode(plane, "a1", active=True, at=101.0)])
    assert _rows(path, "SELECT flight_id, status FROM flights") == [(fid, "live")]
    # A refresh, then the close.
    assert store.upsert_episodes(
        [_episode(plane, "a1", active=True, at=101.0, last=150.0)]
    )
    assert store.upsert_episodes(
        [_episode(plane, "a1", active=False, at=101.0, last=160.0)]
    )
    # A stale refresh never reopens it, and last_updated never moves back.
    assert store.upsert_episodes(
        [_episode(plane, "a1", active=True, at=101.0, last=155.0)]
    )
    rows = _rows(
        path,
        "SELECT active, activated_at, deactivated_at, last_updated, "
        "start_flight_id FROM alerts",
    )
    assert rows == [(0, 101.0, 160.0, 160.0, fid)]
    store.close()


def test_handover_rerecord_keeps_last_updated_and_start(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    old = track_plane(first=100.0, samples=3)
    new = track_plane(first=200.0, samples=3)
    store.upsert_episodes([_episode(old, "a1", active=True, at=101.0, last=190.0)])
    flight, doc = _episode(new, "a1", active=True, at=101.0, last=101.0)
    assert store.upsert_episodes([(flight, doc)])
    assert _rows(
        path, "SELECT flight_id, last_updated, start_flight_id FROM alerts"
    ) == [(new.flight_id, 190.0, old.flight_id)]
    store.close()


def test_discard_deletes_the_flight_and_its_own_episodes(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    kept = track_plane(first=100.0, samples=3)
    dropped = track_plane(first=200.0, samples=3)
    store.upsert_episodes(
        [
            # Began on the kept segment, ended on the dropped one.
            _episode(kept, "carried", active=True, at=101.0),
            _episode(dropped, "own", active=False, at=201.0),
        ]
    )
    assert store.finalize_plane(kept)
    store.upsert_episodes(
        [_episode(dropped, "carried", active=False, at=101.0, last=210.0)]
    )
    assert store.checkpoint([_slice(dropped)], now=300.0)

    assert store.discard_flight(dropped.flight_id)
    assert _rows(path, "SELECT flight_id FROM flights") == [(kept.flight_id,)]
    assert _rows(
        path, "SELECT COUNT(*) FROM telemetry WHERE flight_id = ?", dropped.flight_id
    ) == [(0,)]
    assert _rows(path, "SELECT alert_id, flight_id, active FROM alerts") == [
        ("carried", kept.flight_id, 0)
    ]
    store.close()


def test_discard_drops_a_carried_episode_whose_start_is_gone(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    first = track_plane(first=100.0, samples=3)
    second = track_plane(first=200.0, samples=3)
    store.upsert_episodes([_episode(first, "a1", active=True, at=101.0)])
    # A split hands the open episode over to the next segment first...
    store.upsert_episodes([_episode(second, "a1", active=True, at=101.0)])
    store.discard_flight(first.flight_id)  # ...then the old one may go
    assert _rows(path, "SELECT flight_id, start_flight_id FROM alerts") == [
        (second.flight_id, first.flight_id)
    ]
    store.upsert_episodes([_episode(second, "a1", active=False, at=101.0, last=250)])
    store.discard_flight(second.flight_id)
    assert _rows(path, "SELECT COUNT(*) FROM alerts") == [(0,)]
    assert _rows(path, "SELECT COUNT(*) FROM flights") == [(0,)]
    store.close()


def test_discard_keeps_a_flight_with_an_open_episode(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    plane = track_plane(first=100.0, samples=3)
    store.upsert_episodes([_episode(plane, "a1", active=True, at=101.0)])
    assert store.checkpoint([_slice(plane)], now=300.0)
    assert store.discard_flight(plane.flight_id)
    assert _rows(path, "SELECT status FROM flights") == [("live",)]
    assert _rows(path, "SELECT COUNT(*) FROM telemetry") == [(3,)]
    assert _rows(path, "SELECT active FROM alerts") == [(1,)]
    store.close()


def test_flight_alerts_and_plane_from_rows_round_trip(tmp_path):
    path = tmp_path / "a.db"
    store = ArchiveWriter(path)
    plane = with_unknown_then_ok(track_plane(first=100.0, samples=4))
    plane.track.previous_flight_id = "abc123-50"
    store.checkpoint([_slice(plane)], now=500.0)
    store.upsert_episodes([_episode(plane, "a1", active=True, at=101.0)])
    alerts = store.flight_alerts(plane.flight_id)
    assert [(a["alert_id"], a["active"], a["zone"]) for a in alerts] == [
        ("a1", True, "pad")
    ]
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        frow = conn.execute("SELECT * FROM flights").fetchone()
        doc = {**flight_from_row(frow), "previous_flight_id": frow["previous_flight_id"]}
        points = [
            telemetry_from_row(r)
            for r in conn.execute("SELECT * FROM telemetry ORDER BY timestamp")
        ]
    conn.close()
    rebuilt = plane_from_rows(doc, points)
    assert rebuilt.flight_id == plane.flight_id
    assert rebuilt.track.previous_flight_id == "abc123-50"
    assert [d.time for d in rebuilt.received_data[STORE_LAT]] == [
        100.0,
        101.0,
        102.0,
        103.0,
    ]
    assert rebuilt.track.position_receivers[101.0] == ("roof", "mast")
    assert [v.status for v in rebuilt.track.integrity_log] == ["unknown", "ok"]
    store.close()
