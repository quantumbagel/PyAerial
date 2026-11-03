"""The engine checkpoints live flights into the archive (``FlightArchiver.checkpoint``):
settled samples and new verdicts every CHECKPOINT_SECONDS, cursors advanced
only after a commit, and finalize turning the live row into the final one."""

from __future__ import annotations

import sqlite3
import time

import pytest

from helpers import make_config, make_plane, track_plane, with_unknown_then_ok
from pyaerial.config.schema import ArchiveConfig
from pyaerial.engine import Engine
from pyaerial.engine import archiver as archiver_mod
from pyaerial.store.archive import ArchiveReader
from pyaerial.store.archive.writer import ArchiveWriter


@pytest.fixture
def engine(tmp_path):
    config = make_config()
    config.database.path = str(tmp_path / "pyaerial.db")
    eng = Engine(
        config,
        isolated=True,
        aircraft_db_path=str(tmp_path / "aircraft.db"),
        history_store=ArchiveWriter(config.database.path),
    )
    yield eng
    eng.shutdown()


def _rows(engine, sql, *args):
    with sqlite3.connect(engine.config.database.path) as conn:
        rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def test_checkpoint_writes_settled_samples_then_the_rest(engine):
    now = time.time()
    plane = with_unknown_then_ok(track_plane(first=now - 30, samples=30))
    engine.tracker.planes["ABC123"] = plane
    assert engine.archiver.checkpoint(now, force=True)
    settled = now - archiver_mod.SETTLE_SECONDS
    assert _rows(engine, "SELECT status, checkpoint_at FROM flights") == [("live", now)]
    (count,) = _rows(engine, "SELECT COUNT(*) FROM telemetry")[0]
    assert count == sum(1 for n in range(30) if now - 30 + n <= settled)
    assert plane.track.archived_through == pytest.approx(now - 30 + count - 1)
    assert plane.track.verdicts_through == plane.track.integrity_log[-1].time
    # The live row is hidden from history until finalized.
    reader = ArchiveReader(engine.config.database.path)
    assert reader.count_flights() == 0

    later = now + archiver_mod.SETTLE_SECONDS
    assert engine.archiver.checkpoint(later, force=True)
    assert _rows(engine, "SELECT COUNT(*) FROM telemetry") == [(30,)]

    engine.tracker.remove_planes(["ABC123"])
    assert engine.archiver.finalize(plane)
    assert _rows(engine, "SELECT status FROM flights") == [("completed",)]
    assert _rows(engine, "SELECT COUNT(*) FROM telemetry") == [(30,)]
    assert reader.count_flights() == 1
    reader.close()


def test_checkpoint_is_rate_limited_and_skips_planes_without_a_position(engine):
    now = time.time()
    engine.tracker.planes["ABC123"] = track_plane(first=now - 30, samples=30)
    engine.tracker.planes["DEF456"] = make_plane("def456", now)
    assert engine.archiver.checkpoint(now)
    assert not engine.archiver.checkpoint(now + 1)  # within CHECKPOINT_SECONDS
    assert _rows(engine, "SELECT flight_id FROM flights") == [
        (engine.tracker.planes["ABC123"].flight_id,)
    ]


def test_failed_checkpoint_keeps_cursors_and_backs_off(engine, monkeypatch):
    now = time.time()
    plane = track_plane(first=now - 30, samples=30)
    engine.tracker.planes["ABC123"] = plane
    monkeypatch.setattr(engine.history_store, "checkpoint", lambda *a, **k: False)
    assert not engine.archiver.checkpoint(now, force=True)
    assert plane.track.archived_through is None
    assert plane.track.checkpoint_at is None
    assert not engine.archiver.checkpoint(now + 60)  # backing off
    monkeypatch.undo()
    engine.archiver.retry_now()
    assert engine.archiver.checkpoint(now + 60, force=True)
    assert plane.track.archived_through is not None


def test_tick_checkpoints(engine):
    now = time.time()
    engine.tracker.planes["ABC123"] = track_plane(first=now - 30, samples=30)
    engine.tick()
    assert _rows(engine, "SELECT status FROM flights") == [("live",)]


def test_unretained_checkpointed_flight_is_deleted_at_finalize(tmp_path):
    config = make_config(archive=ArchiveConfig(flights="rules"))
    config.database.path = str(tmp_path / "pyaerial.db")
    engine = Engine(
        config,
        isolated=True,
        aircraft_db_path=str(tmp_path / "aircraft.db"),
        history_store=ArchiveWriter(config.database.path),
    )
    try:
        now = time.time()
        plane = track_plane(first=now - 30, samples=30)
        engine.tracker.planes["ABC123"] = plane
        assert engine.archiver.checkpoint(now, force=True)
        assert _rows(engine, "SELECT COUNT(*) FROM telemetry")[0][0] > 0
        engine.tracker.remove_planes(["ABC123"])
        assert engine.archiver.finalize(plane)
        for table in ("flights", "telemetry", "integrity"):
            assert _rows(engine, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    finally:
        engine.shutdown()


def test_split_starts_a_new_live_row_linked_to_the_segment(engine):
    now = time.time()
    plane = track_plane(first=now - 100, samples=100)
    engine.tracker.planes["ABC123"] = plane
    old_id = plane.flight_id
    assert engine.archiver.checkpoint(now, force=True)
    engine.archiver.max_flight_seconds = 50
    assert engine.archiver.split_if_needed(plane)
    assert plane.flight_id != old_id
    assert plane.track.previous_flight_id == old_id
    assert plane.track.archived_through is None
    assert engine.archiver.checkpoint(now + 60, force=True)
    assert _rows(
        engine,
        "SELECT flight_id, status, previous_flight_id FROM flights ORDER BY start_time",
    ) == [(old_id, "completed", None), (plane.flight_id, "live", old_id)]


def test_checkpoints_are_spread_over_the_ticks(engine):
    """Each call writes the most overdue share of the flights for the time
    since the previous one, so every flight is checkpointed every
    CHECKPOINT_SECONDS without one big burst, at any tick rate."""
    now = time.time()
    for n in range(20):
        engine.tracker.planes[f"A{n:05d}"] = track_plane(
            f"a{n:05d}", first=now - 30, samples=30
        )
    period = archiver_mod.CHECKPOINT_SECONDS
    engine.archiver._last_checkpoint_call = now - 0.5
    sizes = []
    for tick in range(10):  # 2 Hz: a tenth of the flights each
        before = len(_rows(engine, "SELECT flight_id FROM flights"))
        assert engine.archiver.checkpoint(now + tick * 0.5)
        sizes.append(len(_rows(engine, "SELECT flight_id FROM flights")) - before)
    assert sizes == [2] * 10
    # Nothing is due again until CHECKPOINT_SECONDS after its last one.
    assert not engine.archiver.checkpoint(now + 4.9)
    assert engine.archiver.checkpoint(now + period)
    assert engine.tracker.planes["A00000"].track.checkpoint_at == now + period


def test_slow_ticks_write_bigger_slices(engine):
    """Ticks running at 0.5 Hz instead of 2 Hz still checkpoint every flight
    within CHECKPOINT_SECONDS (the loss bound does not stretch)."""
    now = time.time()
    for n in range(20):
        engine.tracker.planes[f"A{n:05d}"] = track_plane(
            f"a{n:05d}", first=now - 30, samples=30
        )
    period = archiver_mod.CHECKPOINT_SECONDS
    engine.archiver._last_checkpoint_call = now - 2.0
    for tick in range(3):
        engine.archiver.checkpoint(now + tick * 2.0)
    # 2 s of a 5 s period per call: 8, 8, then the last 4.
    assert len(_rows(engine, "SELECT flight_id FROM flights")) == 20
    for plane in engine.tracker.planes.values():
        assert plane.track.checkpoint_at - now <= period
