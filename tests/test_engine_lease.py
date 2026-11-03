"""Fencing between tracking engines on one archive (``engine_lease``): an
engine that stalled past its lease and was replaced can no longer write, and
a live engine's archive is never taken over."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from helpers import make_config, track_plane
from pyaerial.engine import Engine
from pyaerial.store.archive import ArchiveWriter, LeaseLost
from pyaerial.store.archive import writer as writer_mod
from pyaerial.store.archive.writer import FlightCheckpoint
from pyaerial.store.codec import build_telemetry_docs, flight_row, integrity_rows


def _rows(path, sql, *args):
    with sqlite3.connect(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def _expire_lease(path) -> None:
    """As if the lease holder stopped renewing a minute ago (stall, kill)."""
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE engine_lease SET renewed_at = renewed_at - 60")
    conn.close()


def _slice(plane):
    fid, icao = plane.flight_id, plane.icao
    return FlightCheckpoint(
        plane, build_telemetry_docs(plane, fid, icao), integrity_rows(plane, fid, icao)
    )


def _episode(plane):
    return (
        flight_row(plane),
        {
            "alert_id": "A1",
            "flight_id": plane.flight_id,
            "icao": plane.icao,
            "zone": "pad",
            "rule": "warn",
            "active": True,
            "activated_at": 1.0,
        },
    )


def _engine(path, **kwargs) -> Engine:
    config = make_config()
    config.database.path = str(path)
    return Engine(
        config,
        isolated=True,
        aircraft_db_path=str(path) + ".aircraft",
        history_store=ArchiveWriter(path),
        **kwargs,
    )


def test_a_live_lease_is_never_taken_over(tmp_path):
    path = tmp_path / "a.db"
    first = ArchiveWriter(path)
    assert first.take_lease("a")
    second = ArchiveWriter(path)
    with pytest.raises(LeaseLost):
        second.take_lease("b", wait=0.0)
    assert second.lease_lost and not second.lease_held
    plane = track_plane(first=100.0, samples=3)
    assert not second.checkpoint([_slice(plane)], now=200.0)
    assert _rows(path, "SELECT COUNT(*) FROM flights") == [(0,)]
    assert first.checkpoint([_slice(plane)], now=200.0)
    first.close()
    second.close()


def test_a_stalled_engine_is_fenced_once_replaced(tmp_path):
    path = tmp_path / "a.db"
    stale = ArchiveWriter(path)
    assert stale.take_lease("a")
    plane = track_plane(first=100.0, samples=3)
    assert stale.checkpoint([_slice(plane)], now=200.0)
    _expire_lease(path)
    fresh = ArchiveWriter(path)
    assert fresh.take_lease("b")
    assert fresh.interrupt_flight(plane.flight_id)
    # Every write of the stalled engine is refused, in its own transaction.
    later = track_plane(first=100.0, samples=6)
    assert not stale.checkpoint([_slice(later)], now=300.0)
    assert stale.lease_lost and not stale.lease_held
    assert not stale.upsert_episodes([_episode(later)])
    assert not stale.finalize_plane(later)
    assert not stale.discard_flight(plane.flight_id)
    assert not stale.renew_lease()
    assert _rows(path, "SELECT status FROM flights") == [("interrupted",)]
    assert _rows(path, "SELECT COUNT(*) FROM telemetry") == [(3,)]
    assert _rows(path, "SELECT COUNT(*) FROM alerts") == [(0,)]
    # It never takes the lease back on its own.
    stale.release_lease()
    assert _rows(path, "SELECT token FROM engine_lease") == [("b",)]
    stale.close()
    fresh.close()


def test_unfenced_writers_are_not_affected(tmp_path):
    """``pyaerial reset`` and tools write without a lease."""
    path = tmp_path / "a.db"
    engine = ArchiveWriter(path)
    assert engine.take_lease("a")
    tool = ArchiveWriter(path)
    assert tool.checkpoint([_slice(track_plane(first=100.0))], now=200.0)
    engine.close()
    tool.close()


def test_engine_refuses_to_start_on_a_live_archive(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    other = ArchiveWriter(path)
    assert other.take_lease("other engine")
    monkeypatch.setattr(writer_mod, "LEASE_TTL_SECONDS", 0.5)
    stop = threading.Event()

    def renew() -> None:  # a running engine renews every tick
        while not stop.wait(0.05):
            other.renew_lease()

    renewer = threading.Thread(target=renew)
    renewer.start()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Another tracking engine is writing"):
            _engine(path)
        assert time.monotonic() - started < 3
    finally:
        stop.set()
        renewer.join()
    other.close()


def test_engine_waits_out_a_lease_that_stops_being_renewed(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    killed = ArchiveWriter(path)
    assert killed.take_lease("killed engine")
    killed.close()
    monkeypatch.setattr(writer_mod, "LEASE_TTL_SECONDS", 0.5)
    engine = _engine(path)
    try:
        assert engine.history_store.lease_held
        assert _rows(path, "SELECT token FROM engine_lease") == [(engine.run_token,)]
    finally:
        engine.shutdown()


def test_graceful_shutdown_releases_the_lease(tmp_path):
    path = tmp_path / "a.db"
    _engine(path).shutdown()
    assert _rows(path, "SELECT COUNT(*) FROM engine_lease") == [(0,)]
    started = time.monotonic()
    _engine(path).shutdown()  # no wait for the previous engine's lease
    assert time.monotonic() - started < 2


def test_replaced_engine_yields_without_writing_or_alerting(tmp_path, monkeypatch):
    """Engine A stalls past its lease; B takes the archive over and recovers
    A's flight. A notices before its next tick and stops without writing."""
    path = tmp_path / "a.db"
    a = _engine(path)
    plane = track_plane(first=time.time() - 30, samples=30)
    a.tracker.planes["ABC123"] = plane
    assert a.archiver.checkpoint(force=True)
    _expire_lease(path)
    b = _engine(path)
    assert _rows(path, "SELECT status FROM flights") == [("interrupted",)]
    finalized: list[str] = []
    monkeypatch.setattr(
        a.archiver, "finalize", lambda p: finalized.append(p.flight_id) or True
    )
    ticks: list[int] = []
    monkeypatch.setattr(a, "tick", lambda: ticks.append(1) or 0)
    runner = threading.Thread(target=a.run)
    runner.start()
    runner.join(10)
    assert not runner.is_alive()
    assert a.yielded_writer and ticks == [] and finalized == []
    assert a.history_store.lease_lost
    assert _rows(path, "SELECT token FROM engine_lease") == [(b.run_token,)]
    b.shutdown()


def test_yielding_redis_but_not_the_archive_still_archives(tmp_path):
    """Another engine took Redis (on another archive): this one still owns
    its archive, so its in-flight flights are finalized there."""
    path = tmp_path / "a.db"
    engine = _engine(path)
    plane = track_plane(first=time.time() - 30, samples=30)
    engine.tracker.planes["ABC123"] = plane
    engine.yielded_writer = True
    engine.shutdown()
    assert _rows(path, "SELECT status FROM flights") == [("completed",)]
    assert _rows(path, "SELECT COUNT(*) FROM engine_lease") == [(0,)]
