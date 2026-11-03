"""Alert episodes are recorded in the archive as they happen
(``store.recorder.EpisodeRecorder``): activation and deactivation at once with
their flight row, while-active refreshes with the next checkpoint, and an
outbox for writes the archive refused."""

from __future__ import annotations

import sqlite3
import time

import pytest

from helpers import make_config, track_plane
from pyaerial.constants import STORE_ICAO
from pyaerial.engine import Engine
from pyaerial.store.archive import ArchiveReader
from pyaerial.store.archive.writer import ArchiveWriter

_META = {STORE_ICAO: "abc123", "zone": "pad", "type": "warn"}


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


def _plane(engine):
    plane = track_plane(first=time.time() - 30, samples=30)
    engine.tracker.planes["ABC123"] = plane
    return plane


def test_activation_and_deactivation_reach_the_archive_at_once(engine):
    plane = _plane(engine)
    fid = plane.flight_id
    rec = engine.episodes
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    assert _rows(engine, "SELECT flight_id, status FROM flights") == [(fid, "live")]
    assert _rows(engine, "SELECT alert_id, active, start_flight_id FROM alerts") == [
        ("A1", 1, fid)
    ]
    # Hidden from history while the flight is live.
    reader = ArchiveReader(engine.config.database.path)
    assert reader.get_flight(fid) is None
    assert reader.get_alerts() == [] and reader.count_alerts() == 0

    # A refresh waits for the checkpoint.
    rec.update_active_alert(plane, "A1", _META, {}, 150.0)
    assert _rows(engine, "SELECT last_updated FROM alerts") == [(100.0,)]
    assert engine.archiver.checkpoint(force=True)
    assert _rows(engine, "SELECT last_updated FROM alerts") == [(150.0,)]

    rec.record_alert_episode(
        plane,
        _META,
        {},
        alert_id="A1",
        activated_at=100.0,
        active=False,
        deactivated_at=160.0,
    )
    assert _rows(engine, "SELECT active, deactivated_at FROM alerts") == [(0, 160.0)]
    assert rec.checkpoint_episodes() == []

    engine.tracker.remove_planes(["ABC123"])
    assert engine.archiver.finalize(plane)
    assert [a["alert_id"] for a in reader.get_alerts()] == ["A1"]
    assert reader.get_flight(fid)["status"] == "completed"
    reader.close()


def test_refused_episode_waits_in_the_outbox_until_a_checkpoint(engine, monkeypatch):
    plane = _plane(engine)
    rec = engine.episodes
    cached: list[str] = []
    monkeypatch.setattr(
        engine.live_store, "cache_episode", lambda doc: cached.append(doc["alert_id"])
    )
    monkeypatch.setattr(engine.history_store, "upsert_episodes", lambda e: False)
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    rec.record_alert_episode(
        plane,
        _META,
        {},
        alert_id="A1",
        activated_at=100.0,
        active=False,
        deactivated_at=120.0,
    )
    assert list(rec.outbox) == ["A1"]
    assert rec.outbox["A1"][1]["active"] is False  # latest state only
    assert _rows(engine, "SELECT COUNT(*) FROM alerts") == [(0,)]
    # The live cache was still written.
    assert cached == ["A1", "A1"]
    monkeypatch.undo()
    assert engine.archiver.checkpoint(force=True)
    assert rec.outbox == {}
    assert _rows(engine, "SELECT alert_id, active, deactivated_at FROM alerts") == [
        ("A1", 0, 120.0)
    ]


def test_drain_resends_the_outbox(engine, monkeypatch):
    plane = _plane(engine)
    rec = engine.episodes
    monkeypatch.setattr(engine.history_store, "upsert_episodes", lambda e: False)
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    assert not rec.drain()
    monkeypatch.undo()
    assert rec.drain()
    assert _rows(engine, "SELECT alert_id FROM alerts") == [("A1",)]


def test_a_failing_archive_write_still_updates_the_live_cache(engine, monkeypatch):
    plane = _plane(engine)

    def boom(_entries):
        raise RuntimeError("bug")

    cached: list[dict] = []
    monkeypatch.setattr(engine.live_store, "cache_episode", cached.append)
    monkeypatch.setattr(engine.history_store, "upsert_episodes", boom)
    engine.episodes.record_alert_episode(
        plane, _META, {}, alert_id="A1", activated_at=100.0
    )
    assert "A1" in engine.episodes.outbox
    assert [doc["alert_id"] for doc in cached] == ["A1"]


def test_handover_rerecord_keeps_last_updated(engine):
    plane = _plane(engine)
    rec = engine.episodes
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    rec.update_active_alert(plane, "A1", _META, {}, 150.0)
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    (entry,) = rec.checkpoint_episodes()
    assert entry[1]["last_updated"] == 150.0
    assert [doc["alert_id"] for doc in rec.open_docs(plane.flight_id)] == ["A1"]


# --- Outbox: bounded, copied to Redis while the archive is down --------------


def test_outbox_is_bounded_and_counts_drops(engine, monkeypatch):
    from pyaerial.store import recorder as recorder_mod

    monkeypatch.setattr(recorder_mod, "OUTBOX_MAX", 2)
    monkeypatch.setattr(engine.history_store, "upsert_episodes", lambda e: False)
    plane = _plane(engine)
    for n in range(3):
        engine.episodes.record_alert_episode(
            plane, _META, {}, alert_id=f"A{n}", activated_at=100.0 + n
        )
    assert list(engine.episodes.outbox) == ["A1", "A2"]
    assert engine.episodes.dropped == 1
    assert engine._counters()["episodes_dropped"] == 1


def test_outbox_is_copied_to_redis_until_archived(fake, tmp_path, monkeypatch):
    from helpers import LOCAL_REDIS_URI
    from pyaerial.store.live.writer import LiveWriter
    from pyaerial.store.recorder import EpisodeRecorder

    archive = ArchiveWriter(tmp_path / "a.db")
    live = LiveWriter(LOCAL_REDIS_URI)
    rec = EpisodeRecorder(archive, live)
    plane = track_plane(first=time.time() - 30, samples=30)
    monkeypatch.setattr(archive, "upsert_episodes", lambda e: False)
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    assert set(fake.hashes["live:episode_outbox"]) == {"A1"}
    # A cache clear at startup keeps it; only `pyaerial reset` drops it.
    live.clear_cache()
    assert set(fake.hashes["live:episode_outbox"]) == {"A1"}
    monkeypatch.undo()
    assert rec.drain()
    assert not fake.hashes.get("live:episode_outbox")
    archive.close()


def test_crash_during_an_outage_replays_the_outbox_and_closes_once(
    fake, tmp_path, monkeypatch
):
    from helpers import pad_config
    from pyaerial.alerts.engine import AlertEngine
    from pyaerial.config.schema import AlertActionConfig
    from pyaerial.store.codec import flight_row

    config = pad_config(tmp_path)
    config.zones["pad"].rules[0].on_deactivate = [AlertActionConfig(method="print")]
    plane = track_plane(first=time.time() - 600, samples=30)
    doc = {
        "alert_id": "A1",
        "flight_id": plane.flight_id,
        "icao": "abc123",
        "zone": "pad",
        "rule": "warn",
        "active": True,
        "activated_at": plane.track.first_packet + 5,
        "deactivated_at": None,
        "last_updated": plane.track.first_packet + 20,
        "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
    }
    import json

    fake.hashes["live:episode_outbox"] = {"A1": json.dumps([flight_row(plane), doc])}
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        AlertEngine,
        "_run_actions",
        lambda self, actions, meta, payload, alert_id: sent.append(
            (meta["reason"]["hook"], meta["reason"].get("ended_by"))
        ),
    )
    engine = Engine(config, aircraft_db_path=str(tmp_path / "aircraft.db"))
    engine.shutdown()
    assert sent == [("deactivate", "restart")]
    with sqlite3.connect(config.database.path) as conn:
        alerts = conn.execute("SELECT alert_id, active FROM alerts").fetchall()
        flights = conn.execute("SELECT status FROM flights").fetchall()
    conn.close()
    assert alerts == [("A1", 0)]
    assert flights == [("interrupted",)]  # a retain rule alerted on it
    assert not fake.hashes.get("live:episode_outbox")


# --- A bad outbox entry never wedges the archive -----------------------------


def test_poisoned_redis_outbox_entry_is_dropped_at_restore(fake, tmp_path, caplog):
    """A flight row without start_time used to fail every checkpoint, recovery
    and finalize for good (the reviewer's probe)."""
    from helpers import pad_config
    from pyaerial.store.codec import flight_row

    config = pad_config(tmp_path)
    plane = track_plane(first=time.time() - 600, samples=30)
    bad_row = flight_row(plane)
    del bad_row["start_time"]
    doc = {
        "alert_id": "BAD",
        "flight_id": plane.flight_id,
        "icao": "abc123",
        "zone": "pad",
        "rule": "warn",
        "active": True,
        "activated_at": 1.0,
        "last_updated": 2.0,
    }
    import json

    fake.hashes["live:episode_outbox"] = {"BAD": json.dumps([bad_row, doc])}
    engine = Engine(config, aircraft_db_path=str(tmp_path / "a.db"))
    try:
        assert engine.episodes.outbox == {}
        assert engine.episodes.dropped == 1
        assert engine._counters()["episodes_dropped"] == 1
        assert not fake.hashes.get("live:episode_outbox")
        assert engine.archiver._recovered
        mine = track_plane(first=time.time() - 60, samples=30)
        mine.info.icao = "def456"
        engine.tracker.planes["DEF456"] = mine
        engine.tick()
        assert _rows(engine, "SELECT status FROM flights") == [("live",)]
        engine.tracker.planes.clear()
        assert engine.archiver.finalize(mine)
        assert _rows(engine, "SELECT status FROM flights") == [("completed",)]
    finally:
        engine.shutdown()


def test_rejected_entry_is_retried_alone_then_quarantined(engine, monkeypatch, caplog):
    from pyaerial.store import recorder as recorder_mod
    from pyaerial.store.archive import RowRejected

    good = _plane(engine)
    other = track_plane(first=time.time() - 30, samples=30)
    other.info.icao = "def456"
    rec = engine.episodes
    down = [True]
    real = engine.history_store.upsert_episodes

    def upsert(entries):
        if down[0]:
            return False
        if any(doc["alert_id"] == "BAD" for _flight, doc in entries):
            raise RowRejected("IntegrityError: bad row")
        return real(entries)

    monkeypatch.setattr(engine.history_store, "upsert_episodes", upsert)
    rec.record_alert_episode(good, _META, {}, alert_id="A1", activated_at=100.0)
    rec.record_alert_episode(other, _META, {}, alert_id="BAD", activated_at=100.0)
    assert list(rec.outbox) == ["A1", "BAD"]
    assert rec.drain() is False  # an outage: nothing counted against them
    assert rec.dropped == 0 and list(rec.outbox) == ["A1", "BAD"]
    down[0] = False
    # The checkpoint writes the good entry and carries on (BAD's flight is
    # not tracked, so no refresh supersedes its entry).
    assert engine.archiver.checkpoint()
    assert list(rec.outbox) == ["BAD"]
    assert _rows(engine, "SELECT alert_id FROM alerts") == [("A1",)]
    with caplog.at_level("ERROR", logger="pyaerial.store"):
        for _ in range(recorder_mod.QUARANTINE_AFTER):
            rec.drain()
    assert rec.outbox == {} and rec.dropped == 1
    assert "rejected it 3 times" in caplog.text


def test_finalize_waits_only_for_its_own_unarchived_episodes(engine, monkeypatch):
    from pyaerial.store.archive import RowRejected

    plane = _plane(engine)
    other = track_plane(first=time.time() - 30, samples=30)
    other.info.icao = "def456"
    real = engine.history_store.upsert_episodes
    calls: list[set[str]] = []

    def upsert(entries):
        calls.append({doc["flight_id"] for _flight, doc in entries})
        if any(doc["flight_id"] == other.flight_id for _flight, doc in entries):
            raise RowRejected("bad")
        return real(entries)

    monkeypatch.setattr(engine.history_store, "upsert_episodes", upsert)
    engine.episodes.record_alert_episode(
        other, _META, {}, alert_id="B1", activated_at=100.0
    )
    calls.clear()
    engine.tracker.remove_planes(["ABC123"])
    assert engine.archiver.finalize(plane)
    assert all(other.flight_id not in flights for flights in calls)
    assert "B1" in engine.episodes.outbox


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f, d: f.pop("start_time"),
        lambda f, d: f.update(start_time="soon"),
        lambda f, d: d.pop("alert_id"),
        lambda f, d: d.update(flight_id="other-1"),
        lambda f, d: d.update(active="yes"),
        lambda f, d: d.update(activated_at=None),
        lambda f, d: f.update(info={"not": "json text"}),
    ],
)
def test_valid_entry_rejects_incomplete_entries(mutate):
    from pyaerial.store.codec import alert_episode_doc, flight_row
    from pyaerial.store.recorder import valid_entry

    plane = track_plane(first=100.0, samples=3)
    flight = flight_row(plane)
    doc = alert_episode_doc(plane, _META, {}, alert_id="A1", activated_at=101.0, active=True)
    assert valid_entry([dict(flight), dict(doc)]) is not None
    mutate(flight, doc)
    assert valid_entry([flight, doc]) is None


def test_start_segment_survives_an_outage_across_a_handover(engine, monkeypatch):
    """The activation waits in the outbox, the episode is handed over to the
    next segment, and only its close reaches the archive: the row must still
    name the segment where it began."""
    plane = _plane(engine)
    first = plane.flight_id
    rec = engine.episodes
    real = engine.history_store.upsert_episodes
    monkeypatch.setattr(engine.history_store, "upsert_episodes", lambda e: False)
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    plane.track.first_packet += 3_600  # a segment split
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    rec.record_alert_episode(
        plane, _META, {}, alert_id="A1", activated_at=100.0, active=False,
        deactivated_at=200.0,
    )
    assert rec.outbox["A1"][1]["start_flight_id"] == first
    monkeypatch.setattr(engine.history_store, "upsert_episodes", real)
    assert rec.drain()
    assert _rows(engine, "SELECT flight_id, start_flight_id, active FROM alerts") == [
        (plane.flight_id, first, 0)
    ]
