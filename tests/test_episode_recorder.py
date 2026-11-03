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
from pyaerial.store.archive import ArchiveReader, ArchiveWriter

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
    assert [a["alert_id"] for a in engine.live_store.get_alerts(active_only=False)] == [
        "A1"
    ]
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

    monkeypatch.setattr(engine.history_store, "upsert_episodes", boom)
    engine.episodes.record_alert_episode(
        plane, _META, {}, alert_id="A1", activated_at=100.0
    )
    assert "A1" in engine.episodes.outbox
    assert engine.live_store.get_alerts(active_only=True)[0]["alert_id"] == "A1"


def test_handover_rerecord_keeps_last_updated(engine):
    plane = _plane(engine)
    rec = engine.episodes
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    rec.update_active_alert(plane, "A1", _META, {}, 150.0)
    rec.record_alert_episode(plane, _META, {}, alert_id="A1", activated_at=100.0)
    (entry,) = rec.checkpoint_episodes()
    assert entry[1]["last_updated"] == 150.0
    assert rec.open_on("A1") == plane.flight_id


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
    from pyaerial.store.live import LiveWriter
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
