"""Long flights archived as segments: splits never lose the track or its
alert episodes, and tracking continues on the live plane."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from helpers import (
    LOCAL_REDIS_URI,
    alert_meta,
    fine_track_plane,
    make_config,
    make_rule,
    pad_config,
    pad_plane,
)
from pyaerial.alerts.engine import AlertEngine
from pyaerial.config.schema import ArchiveConfig, ZoneConfig
from pyaerial.constants import (
    STORE_ICAO,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.engine import Engine
from pyaerial.models import Verdict
from pyaerial.store.archive import ArchiveReader, ArchiveWriter
from pyaerial.store.live import LiveWriter

# --- Flights longer than tracking.max_flight_seconds -------------------------


def test_long_flight_is_archived_as_a_segment_and_tracking_continues(
    tmp_path, monkeypatch
):
    config = pad_config(tmp_path)
    engine = Engine(config, isolated=True, aircraft_db_path=str(tmp_path / "a.db"))
    store = ArchiveWriter(config.database.path)
    reader = ArchiveReader(config.database.path)
    real_history = engine.history_store
    engine.history_store = store
    notifications: list[str] = []
    monkeypatch.setattr(
        AlertEngine,
        "_run_actions",
        lambda self, actions, meta, payload, alert_id: notifications.append(alert_id),
    )
    deactivations: list[str] = []
    monkeypatch.setattr(
        engine.calculator.alerts,
        "deactivate",
        lambda plane, ended_by=None: deactivations.append(1),
    )
    try:
        engine._max_flight_seconds = 3_600.0
        now = 1_800_000_000.0
        first = now - 2 * 3_600
        plane = fine_track_plane("abc123", first, samples=721, step=10.0)  # 2 h at 10 s
        plane.track.position_receivers = {first + 7_200: ("roof",)}
        plane.track.integrity_log = [
            Verdict(first + 5, 0.9, "ok", (), {})
        ]
        engine.tracker.planes["ABC123"] = plane
        old_fid = plane.flight_id
        engine.live_store.write_live_planes(engine.tracker.planes)
        engine.live_store.record_alert_episode(
            plane, alert_meta(), {}, alert_id="a1", activated_at=first + 100
        )

        engine._split_long_flights()

        # Segment 1 is archived whole.
        assert len(reader.get_telemetry(old_fid)) == 721
        assert reader.get_flight(old_fid)["end_time"] == first + 7_200
        # Tracking continues as a new flight from the segment boundary.
        live = engine.tracker.planes["ABC123"]
        new_fid = live.flight_id
        assert new_fid != old_fid
        assert live.track.first_packet == first + 7_200
        assert len(live.received_data[STORE_LAT]) == 1
        assert live.track.integrity_log[-1].status == "ok"
        # The active episode carries over silently with its start time.
        assert notifications == [] and deactivations == []
        active = engine.live_store.get_alerts(active_only=True)
        assert [(a["alert_id"], a["flight_id"], a["activated_at"]) for a in active] == [
            ("a1", new_fid, first + 100)
        ]
        assert old_fid not in engine.live_store.flight_ids()
        engine.live_store.update_active_alert(live, "a1", alert_meta(), {}, now + 5)
        assert (
            engine.live_store.get_alerts(active_only=True)[0]["activated_at"]
            == first + 100
        )
        # Not split again while it is short.
        engine._split_long_flights()
        assert engine.tracker.planes["ABC123"].flight_id == new_fid
    finally:
        engine.history_store = real_history
        reader.close()
        store.close()
        engine.shutdown()


def test_max_flight_seconds_zero_disables_segmentation(tmp_path):
    engine = Engine(
        make_config(), isolated=True, aircraft_db_path=str(tmp_path / "a.db")
    )
    try:
        engine._max_flight_seconds = 0.0
        plane = fine_track_plane("abc123", 0.0, samples=3, step=40_000.0)
        engine.tracker.planes["ABC123"] = plane
        engine._split_long_flights()
        assert engine.tracker.planes["ABC123"] is plane
        assert plane.flight_id == "abc123-0"
    finally:
        engine.shutdown()


def test_max_flight_seconds_reads_the_config(tmp_path):
    engine = Engine(
        make_config(), isolated=True, aircraft_db_path=str(tmp_path / "a.db")
    )
    try:
        expected = getattr(engine.config.tracking, "max_flight_seconds", 14_400.0)
        assert engine._max_flight_seconds == expected
    finally:
        engine.shutdown()


# --- Segment splits keep the track and its alert episodes --------------------


_PAD_RING = [
    [35.72, -78.70],
    [35.73, -78.70],
    [35.73, -78.69],
    [35.72, -78.69],
    [35.72, -78.70],
]


def _split_setup(tmp_path, monkeypatch, *, retain: bool = True, archive=None):
    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=_PAD_RING,
                rules=[make_rule(name="warn", retain=retain, dwell_seconds=1)],
            )
        },
        archive=archive,
    )
    config.database.path = str(tmp_path / "pyaerial.db")
    monkeypatch.setattr(AlertEngine, "_run_actions", lambda self, *a, **k: None)
    engine = Engine(config, isolated=True, aircraft_db_path=str(tmp_path / "a.db"))
    store = ArchiveWriter(config.database.path)
    reader = ArchiveReader(config.database.path)
    real = engine.history_store
    engine.history_store = store
    engine._max_flight_seconds = 3_600.0
    now = time.time()
    first = now - 7_200
    plane = pad_plane(first)
    engine.tracker.planes["ABC123"] = plane
    engine.live_store.write_live_planes(engine.tracker.planes)

    def cleanup():
        engine.history_store = real
        reader.close()
        store.close()
        engine._pending_finalize.clear()
        engine.shutdown()

    return engine, reader, plane, first, cleanup


def _activate_in_pad(engine, plane, first):
    old = plane.flight_id
    alerts = engine.calculator.alerts
    alerts._live_flight["abc123"] = old
    alerts._alert_state[("abc123", "pad", "warn")] = {
        "activated_at": first + 100,
        "last_periodic": 0,
        "alert_id": "A1",
        "flight_id": old,
        "eta": 0.0,
    }
    engine.live_store.record_alert_episode(
        plane,
        {STORE_ICAO: "abc123", "zone": "pad", "type": "warn"},
        {},
        alert_id="A1",
        activated_at=first + 100,
    )
    return old


# --- M1: the segment is archived whatever the alert steps do -------------------


@pytest.mark.parametrize("broken", ["handover", "carry"])
def test_segment_is_archived_when_an_alert_step_fails(tmp_path, monkeypatch, broken):
    engine, reader, plane, _first, cleanup = _split_setup(tmp_path, monkeypatch)
    try:
        old = plane.flight_id

        def boom(*_a, **_k):
            raise RuntimeError("bug in alert hand-off")

        if broken == "handover":
            monkeypatch.setattr(engine.calculator, "handover", boom)
        else:
            monkeypatch.setattr(engine.live_store, "carry_active_alerts", boom)
        engine._split_long_flights()
        assert len(reader.get_telemetry(old)) == 721
        assert engine.tracker.planes["ABC123"].flight_id != old
    finally:
        cleanup()


def test_segment_is_kept_for_retry_when_the_archive_fails(tmp_path, monkeypatch):
    engine, reader, plane, _first, cleanup = _split_setup(tmp_path, monkeypatch)
    try:
        old = plane.flight_id
        monkeypatch.setattr(engine.history_store, "finalize_plane", lambda *a, **k: False)
        engine._split_long_flights()
        pending = engine._pending_finalize[old]
        assert len(pending.received_data[STORE_LAT]) == 721
        assert old in engine.live_store.flight_ids()  # live copy not popped
    finally:
        cleanup()


def test_segment_is_archived_when_trimming_the_live_plane_fails(tmp_path, monkeypatch):
    import pyaerial.engine as engine_mod

    engine, reader, plane, _first, cleanup = _split_setup(tmp_path, monkeypatch)
    try:
        old = plane.flight_id

        def broken_trim(*_a, **_k):
            raise RuntimeError("trim bug")

        monkeypatch.setattr(engine_mod.Engine, "_start_new_segment", broken_trim)
        engine._split_long_flights()
        assert len(reader.get_telemetry(old)) == 721
    finally:
        cleanup()


# --- M2: carried episodes are archived with the old segment too ----------------


def test_old_segment_keeps_the_episode_and_its_retain_evidence(tmp_path, monkeypatch):
    # archive.flights: rules and no heading: only the alert can retain it.
    engine, reader, plane, first, cleanup = _split_setup(
        tmp_path, monkeypatch, retain=True, archive=ArchiveConfig(flights="rules")
    )
    try:
        old = _activate_in_pad(engine, plane, first)
        engine._split_long_flights()
        assert reader.get_flight(old) is not None
        assert [a["alert_id"] for a in reader.get_alerts(flight_id=old)] == ["A1"]
        # The live episode continues on the new flight, start time unchanged.
        new = engine.tracker.planes["ABC123"].flight_id
        active = engine.live_store.get_alerts(active_only=True)
        assert [(a["alert_id"], a["flight_id"], a["activated_at"]) for a in active] == [
            ("A1", new, first + 100)
        ]
    finally:
        cleanup()


def test_discarded_continuation_still_updates_the_episode_row(tmp_path, monkeypatch):
    engine, reader, plane, first, cleanup = _split_setup(
        tmp_path, monkeypatch, retain=False
    )
    try:
        old = _activate_in_pad(engine, plane, first)
        engine._split_long_flights()
        new = engine.tracker.planes["ABC123"].flight_id
        # Goes silent right after the split: one sample, below min_positions.
        engine._expire_planes(time.time() + 10_000)
        assert reader.get_flight(new) is None
        rows = (
            sqlite3.connect(reader.path)
            .execute("SELECT alert_id, flight_id, active, deactivated_at FROM alerts")
            .fetchall()
        )
        assert len(rows) == 1
        alert_id, flight_id, active, deactivated_at = rows[0]
        assert (alert_id, flight_id, active) == ("A1", old, 0)
        assert deactivated_at is not None
    finally:
        cleanup()


def test_retained_continuation_moves_the_row_to_the_final_segment(
    tmp_path, monkeypatch
):
    engine, reader, plane, first, cleanup = _split_setup(
        tmp_path, monkeypatch, retain=True
    )
    try:
        _activate_in_pad(engine, plane, first)
        engine._split_long_flights()
        new = engine.tracker.planes["ABC123"].flight_id
        engine._expire_planes(time.time() + 10_000)
        rows = (
            sqlite3.connect(reader.path)
            .execute("SELECT alert_id, flight_id FROM alerts")
            .fetchall()
        )
        assert rows == [("A1", new)]
    finally:
        cleanup()


def test_popping_the_old_segment_keeps_carried_episodes_in_redis(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    first = time.time() - 7_200
    plane = pad_plane(first, samples=3)
    old = plane.flight_id
    store.write_live_planes({"ABC123": plane})
    store.record_alert_episode(
        plane,
        {STORE_ICAO: "abc123", "zone": "pad", "type": "warn"},
        {},
        alert_id="A1",
        activated_at=first + 1,
    )
    plane.track.first_packet = first + 2
    new = plane.flight_id
    store.carry_active_alerts(old, new)
    # The old segment still lists it until it is archived.
    assert [
        a["alert_id"] for a in store.get_alerts(flight_id=old, active_only=False)
    ] == ["A1"]
    store.pop_flight(old)
    assert json.loads(fake.hashes["live:alert_episodes"]["A1"])["flight_id"] == new
    assert json.loads(fake.hashes["live:active_alerts"]["A1"])["flight_id"] == new
    assert "A1" in fake.hashes[f"live:alerts:{new}"]


def test_re_recording_an_active_episode_keeps_last_updated(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = pad_plane(time.time(), samples=2)
    meta = {STORE_ICAO: "abc123", "zone": "pad", "type": "warn"}
    store.record_alert_episode(plane, meta, {}, alert_id="A1", activated_at=5.0)
    store.update_active_alert(plane, "A1", meta, {}, 50.0)
    store.record_alert_episode(
        plane, meta, {}, alert_id="A1", activated_at=5.0
    )  # handover
    doc = store.get_alerts(active_only=True)[0]
    assert doc["activated_at"] == 5.0 and doc["last_updated"] == 50.0
    assert json.loads(fake.hashes["live:active_alerts"]["A1"])["last_updated"] == 50.0


# --- M3: track start survives a split -----------------------------------------


def test_track_started_is_set_once_and_kept_across_splits(tmp_path, monkeypatch):
    engine, _store, plane, first, cleanup = _split_setup(tmp_path, monkeypatch)
    try:
        engine._mark_track_started()
        assert plane.track.track_started == first
        engine._split_long_flights()
        live = engine.tracker.planes["ABC123"]
        assert live.track.first_packet > first
        assert live.track.track_started == first
    finally:
        cleanup()


def test_engine_marks_track_started_for_new_planes(tmp_path, monkeypatch):
    engine = Engine(
        make_config(), isolated=True, aircraft_db_path=str(tmp_path / "a.db")
    )
    try:
        from e2e_support import position_frame

        t = time.time() - 30
        frames = [
            position_frame("abc123", 35.72, -78.69, 300.0, 0),
            position_frame("abc123", 35.7201, -78.6901, 300.0, 1),
        ]
        engine._process_frames([])  # no planes yet: nothing to mark
        from pyaerial.receivers.frames import RawFrame

        engine._process_frames(
            [
                RawFrame(hex=hx, timestamp=t + n * 0.4, receiver="a")
                for n, hx in enumerate(frames)
            ]
        )
        internal = engine.tracker.planes["ABC123"].track
        assert internal.track_started == internal.first_packet
    finally:
        engine.shutdown()


# --- No live gap after a split -----------------------------------------------


def test_new_segment_is_live_before_the_old_one_is_popped(tmp_path, monkeypatch):
    engine, _store, plane, _first, cleanup = _split_setup(tmp_path, monkeypatch)
    try:
        old = plane.flight_id
        engine._split_long_flights()
        new = engine.tracker.planes["ABC123"].flight_id
        ids = engine.live_store.flight_ids()
        assert new in ids and old not in ids
        assert engine.live_store.latest_telemetry(new) is not None
    finally:
        cleanup()


# --- A stale last position does not start the next segment -------------------


def test_stale_last_position_does_not_overlap_segments(tmp_path, monkeypatch):
    engine, reader, plane, first, cleanup = _split_setup(tmp_path, monkeypatch)
    try:
        internal = plane.track
        # Positions stopped 2 h ago; Mode S / velocity kept the plane alive.
        recv = plane.received_data
        recv[STORE_LAT] = recv[STORE_LAT][:10]
        recv[STORE_LONG] = recv[STORE_LONG][:10]
        internal.last_update = first + 7_200
        old = plane.flight_id
        engine._split_long_flights()
        live = engine.tracker.planes["ABC123"]
        assert live.track.first_packet == first + 7_200
        assert not live.received_data.get(STORE_LAT)
        assert len(reader.get_telemetry(old)) == 10
    finally:
        cleanup()
