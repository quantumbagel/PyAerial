"""Startup recovery from the archive (``FlightArchiver.recover_on_startup``):
what a killed engine left ``live`` is closed once, from SQLite alone.

Live flights become ``interrupted`` (ending at their last checkpointed
sample) or are deleted by the archive policy, and their open alert episodes
get exactly one ``ended_by: restart`` deactivate."""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from helpers import make_config, make_rule, pad_config, track_plane
from pyaerial.alerts.engine import AlertEngine
from pyaerial.api import queries
from pyaerial.api.models import FlightSummary
from pyaerial.config.schema import AlertActionConfig, ArchiveConfig, ZoneConfig
from pyaerial.constants import STORE_ICAO
from pyaerial.engine import Engine
from pyaerial.engine import archiver as archiver_mod
from pyaerial.store.archive import ArchiveReader, ArchiveWriter
from pyaerial.store.archive.writer import FlightCheckpoint
from pyaerial.store.codec import build_telemetry_docs, flight_row, integrity_rows

_PAD = [
    [35.72, -78.70],
    [35.73, -78.70],
    [35.73, -78.69],
    [35.72, -78.69],
    [35.72, -78.70],
]


@pytest.fixture
def notified(monkeypatch):
    """(hook, alert_id, ended_by) of every notification the engines send."""
    events: list[tuple[str, str, str | None]] = []

    def record(self, actions, meta, payload, alert_id):
        reason = meta.get("reason") or {}
        events.append((reason.get("hook"), alert_id, reason.get("ended_by")))

    monkeypatch.setattr(AlertEngine, "_run_actions", record)
    return events


def _config(tmp_path, *, flights="all", retain=True):
    rule = make_rule(name="warn", retain=retain, dwell_seconds=1)
    rule.on_activate = [AlertActionConfig(method="print")]
    rule.on_deactivate = [AlertActionConfig(method="print")]
    config = make_config(
        zones={"pad": ZoneConfig(coordinates=_PAD, rules=[rule])},
        archive=ArchiveConfig(flights=flights),
    )
    config.database.path = str(tmp_path / "pyaerial.db")
    return config


def _engine(config) -> Engine:
    return Engine(
        config,
        isolated=True,
        aircraft_db_path=str(config.database.path) + ".aircraft",
        history_store=ArchiveWriter(config.database.path),
    )


def _previous_run(path, plane, *, alert_id: str | None = "A1", until=None):
    """What a killed engine leaves: a checkpointed live flight (samples up
    to *until*) and, optionally, an open episode last refreshed at +20 s."""
    store = ArchiveWriter(path)
    fid, icao = plane.flight_id, plane.icao
    first = plane.track.first_packet
    store.checkpoint(
        [
            FlightCheckpoint(
                plane,
                build_telemetry_docs(plane, fid, icao, until=until),
                integrity_rows(plane, fid, icao),
            )
        ],
        now=first + 30,
    )
    if alert_id is not None:
        doc = {
            "alert_id": alert_id,
            "flight_id": fid,
            "icao": icao,
            "callsign": "SWA1",
            "zone": "pad",
            "rule": "warn",
            "active": True,
            "activated_at": first + 5,
            "deactivated_at": None,
            "last_updated": first + 20,
            "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
        }
        store.upsert_episodes([(flight_row(plane), doc)])
    store.close()


def _rows(path, sql, *args):
    with sqlite3.connect(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def _pad_plane(first):
    plane = track_plane(first=first, samples=30, lat_step=0.0)
    for datum in plane.received_data["latitude"]:
        datum.value = 35.725
    for datum in plane.received_data["longitude"]:
        datum.value = -78.695
    return plane


def test_killed_engines_flight_is_interrupted_and_its_alert_closed_once(
    tmp_path, notified
):
    config = _config(tmp_path)
    first = time.time() - 600
    plane = _pad_plane(first)
    _previous_run(config.database.path, plane, until=first + 20)
    fid = plane.flight_id

    engine = _engine(config)
    try:
        assert notified == [("deactivate", "A1", "restart")]
        assert _rows(config.database.path, "SELECT status, end_time FROM flights") == [
            ("interrupted", first + 20)
        ]
        assert _rows(
            config.database.path, "SELECT active, deactivated_at FROM alerts"
        ) == [(0, first + 20)]
        # History shows it, completed on the wire, marked interrupted.
        reader = ArchiveReader(config.database.path)
        (summary,) = queries.get_history_flights(reader, None)
        assert summary["flight_id"] == fid
        assert summary["status"] == "completed" and summary["interrupted"] is True
        FlightSummary.model_validate(summary)
        detail = queries.get_flight_detail(
            fid, "history", live_store=None, history=reader, aircraft_db=None
        )
        assert detail["status"] == "completed" and detail["interrupted"] is True
        assert [a["alert_id"] for a in reader.get_alerts()] == ["A1"]
        reader.close()
    finally:
        engine.shutdown()

    # Idempotent: a second start finds nothing left open.
    again = _engine(config)
    again.shutdown()
    assert notified == [("deactivate", "A1", "restart")]


def test_finalized_flights_omit_interrupted(tmp_path):
    config = _config(tmp_path)
    engine = _engine(config)
    plane = _pad_plane(time.time() - 60)
    engine.tracker.planes["ABC123"] = plane
    engine.shutdown()
    reader = ArchiveReader(config.database.path)
    (summary,) = queries.get_history_flights(reader, None)
    assert summary["status"] == "completed" and "interrupted" not in summary
    reader.close()


def test_rules_mode_deletes_an_unmatched_interrupted_flight(tmp_path, notified):
    config = _config(tmp_path, flights="rules", retain=False)
    plane = track_plane(first=time.time() - 600, samples=30)  # far from the pad
    _previous_run(config.database.path, plane, alert_id=None)
    engine = _engine(config)
    engine.shutdown()
    for table in ("flights", "telemetry", "integrity", "alerts"):
        assert _rows(config.database.path, f"SELECT COUNT(*) FROM {table}") == [(0,)]


def test_rules_mode_keeps_an_interrupted_flight_a_retain_rule_alerted(
    tmp_path, notified
):
    config = _config(tmp_path, flights="rules", retain=True)
    plane = track_plane(first=time.time() - 600, samples=30)
    _previous_run(config.database.path, plane)
    engine = _engine(config)
    engine.shutdown()
    assert _rows(config.database.path, "SELECT status FROM flights") == [
        ("interrupted",)
    ]
    assert notified == [("deactivate", "A1", "restart")]


def test_open_episode_without_a_live_flight_is_closed_and_archived(tmp_path, notified):
    """The flight row went missing (e.g. a crash between a discard and the
    deactivate): the episode is still closed once, with a bare flight row."""
    config = _config(tmp_path)
    plane = _pad_plane(time.time() - 600)
    _previous_run(config.database.path, plane)
    with sqlite3.connect(config.database.path) as conn:
        conn.execute("DELETE FROM flights")
        conn.execute("DELETE FROM telemetry")
    conn.close()
    engine = _engine(config)
    engine.shutdown()
    assert notified == [("deactivate", "A1", "restart")]
    assert _rows(config.database.path, "SELECT flight_id, status FROM flights") == [
        (plane.flight_id, "interrupted")
    ]
    assert _rows(config.database.path, "SELECT active FROM alerts") == [(0,)]


def test_an_alerter_bug_does_not_stop_recovery(tmp_path, monkeypatch):
    config = _config(tmp_path)
    for n, icao in enumerate(("aaa111", "bbb222")):
        plane = _pad_plane(time.time() - 600 + n)
        plane.info.icao = icao
        _previous_run(config.database.path, plane, alert_id=f"A{n}")

    real = AlertEngine.close_restored

    def boom(self, plane, doc, **kw):
        if doc["alert_id"] == "A0":
            raise RuntimeError("alerter bug")
        return real(self, plane, doc, **kw)

    monkeypatch.setattr(AlertEngine, "close_restored", boom)
    engine = _engine(config)
    engine.shutdown()
    # The other flight is recovered; the one whose alert stayed open waits.
    assert dict(
        _rows(config.database.path, "SELECT icao, status FROM flights")
    ) == {"aaa111": "live", "bbb222": "interrupted"}


def test_recovery_waits_for_the_archive_and_skips_this_runs_flights(
    tmp_path, notified, monkeypatch
):
    config = _config(tmp_path)
    old = _pad_plane(time.time() - 600)
    _previous_run(config.database.path, old)
    store = ArchiveWriter(config.database.path)
    down = [True]
    monkeypatch.setattr(store, "ping", lambda: not down[0] and ArchiveWriter.ping(store))
    engine = Engine(
        config,
        isolated=True,
        aircraft_db_path=str(tmp_path / "a.db"),
        history_store=store,
    )
    try:
        assert notified == []
        # This run's own flight is checkpointed live meanwhile.
        mine = _pad_plane(time.time() - 60)
        mine.info.icao = "def456"
        engine.tracker.planes["DEF456"] = mine
        down[0] = False
        engine.archiver.retry_now()
        engine.archiver.tick()
        assert notified == [("deactivate", "A1", "restart")]
        assert dict(
            _rows(config.database.path, "SELECT flight_id, status FROM flights")
        ) == {old.flight_id: "interrupted", mine.flight_id: "live"}
    finally:
        engine.shutdown()


def test_recovery_stops_and_backs_off_at_the_first_failed_write(tmp_path, monkeypatch):
    config = _config(tmp_path)
    for n in range(3):
        plane = _pad_plane(time.time() - 600 + n)
        plane.info.icao = f"a0000{n}"
        _previous_run(config.database.path, plane, alert_id=None)
    store = ArchiveWriter(config.database.path)
    attempts: list[str] = []

    def failing(flight_id):
        attempts.append(flight_id)
        return False

    monkeypatch.setattr(store, "interrupt_flight", failing)
    engine = Engine(
        config,
        isolated=True,
        aircraft_db_path=str(tmp_path / "a.db"),
        history_store=store,
    )
    try:
        assert len(attempts) == 1
        engine.archiver.tick()  # backing off
        assert len(attempts) == 1
        monkeypatch.undo()
        engine.archiver.retry_now()
        engine.archiver.tick()
        assert _rows(config.database.path, "SELECT DISTINCT status FROM flights") == [
            ("interrupted",)
        ]
    finally:
        engine.shutdown()


def test_long_recovery_keeps_the_heartbeat_fresh(tmp_path, monkeypatch):
    config = _config(tmp_path)
    for n in range(5):
        plane = _pad_plane(time.time() - 600 + n)
        plane.info.icao = f"a0000{n}"
        _previous_run(config.database.path, plane, alert_id=None)
    beats: list[float] = []
    monkeypatch.setattr(archiver_mod, "STARTUP_HEARTBEAT_INTERVAL", 0.0)
    from pyaerial.store.live import NullLiveWriter

    monkeypatch.setattr(
        NullLiveWriter, "touch_engine", lambda self: beats.append(time.monotonic())
    )
    engine = _engine(config)
    engine.shutdown()
    assert len(beats) >= 5


def test_evicted_pending_flight_is_interrupted_once_history_returns(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    engine = _engine(config)
    try:
        plane = _pad_plane(time.time() - 60)
        engine.tracker.planes["ABC123"] = plane
        assert engine.archiver.checkpoint(force=True)
        engine.tracker.remove_planes(["ABC123"])
        monkeypatch.setattr(
            engine.history_store, "finalize_plane", lambda *a, **k: False
        )
        monkeypatch.setattr(archiver_mod, "PENDING_FINALIZE_BUDGET_BYTES", 1)
        assert not engine.archiver.finalize(plane)
        other = _pad_plane(time.time() - 50)
        other.info.icao = "def456"
        assert not engine.archiver.finalize(other)  # evicts the first
        assert list(engine.archiver.pending) == [other.flight_id]
        monkeypatch.undo()
        engine.archiver.retry_now()
        engine.archiver.tick()
        assert dict(
            _rows(config.database.path, "SELECT flight_id, status FROM flights")
        ) == {plane.flight_id: "interrupted", other.flight_id: "completed"}
    finally:
        engine.shutdown()


def test_pre_v4_redis_leftovers_are_discarded_with_a_warning(fake, tmp_path, caplog):
    config = pad_config(tmp_path)
    ArchiveWriter(config.database.path).close()
    with sqlite3.connect(config.database.path) as conn:
        conn.execute("PRAGMA user_version = 3")  # as 0.22 left it
    conn.close()
    fake.sets["live:flights"] = {"abc123-1", "def456-2"}
    fake.kv["live:flight:abc123-1"] = "{}"
    fake.hashes["live:active_alerts"] = {"x": "{}"}
    with caplog.at_level(logging.WARNING, logger="pyaerial.engine"):
        engine = Engine(config, aircraft_db_path=str(tmp_path / "a.db"))
    try:
        assert "Discarded 2 live flight(s) a pre-1.0 PyAerial left" in caplog.text
        assert "live:flights" not in fake.sets
        assert "live:flight:abc123-1" not in fake.kv
        assert "live:active_alerts" not in fake.hashes
        assert "live:engine" in fake.kv  # our own heartbeat stays
    finally:
        engine.shutdown()


def test_current_archive_clears_the_cache_quietly(fake, tmp_path, caplog):
    config = pad_config(tmp_path)
    ArchiveWriter(config.database.path).close()
    fake.sets["live:flights"] = {"abc123-1"}
    with caplog.at_level(logging.WARNING, logger="pyaerial.engine"):
        engine = Engine(config, aircraft_db_path=str(tmp_path / "a.db"))
    engine.shutdown()
    assert "pre-1.0" not in caplog.text
    assert "live:flights" not in fake.sets


def test_close_restored_uses_the_archived_row(tmp_path, notified):
    """The deactivate carries what the archive knows of the flight."""
    config = _config(tmp_path)
    engine = _engine(config)
    try:
        plane = _pad_plane(1_700_000_000.0)
        plane.info.registration = "N123"
        sent: list[dict] = []
        alerts = engine.calculator.alerts
        real_build = alerts._build_meta

        def build(*args, **kwargs):
            meta = real_build(*args, **kwargs)
            sent.append(meta)
            return meta

        alerts._build_meta = build  # type: ignore[method-assign]
        alerts.close_restored(
            plane,
            {
                "alert_id": "A9",
                "flight_id": plane.flight_id,
                STORE_ICAO: "abc123",
                "zone": "pad",
                "rule": "warn",
                "activated_at": 1_700_000_005.0,
                "last_updated": 1_700_000_050.0,
                "latitude": 35.725,
                "longitude": -78.695,
            },
            now=1_700_000_100.0,
        )
        assert sent[0]["registration"] == "N123"
        assert sent[0]["deactivated_at"] == 1_700_000_050.0
        assert sent[0]["flight_id"] == plane.flight_id
    finally:
        engine.shutdown()


# --- An episode that could not be closed is never orphaned --------------------


def test_failed_deactivation_keeps_a_discarded_flight_until_next_start(
    tmp_path, notified, monkeypatch, caplog
):
    """Finalize of a flight the policy discards, after its deactivation
    failed: the flight stays live (not deleted under its open episode), and
    the next start closes the episode with its one deactivate."""
    config = _config(tmp_path, retain=False)
    config.archive = ArchiveConfig(flights="all", min_positions=1000)
    engine = _engine(config)
    plane = _pad_plane(time.time() - 60)
    engine.tracker.planes["ABC123"] = plane
    engine.episodes.record_alert_episode(
        plane,
        {STORE_ICAO: "abc123", "zone": "pad", "type": "warn"},
        {},
        alert_id="X1",
        activated_at=time.time() - 30,
    )
    engine.tracker.remove_planes(["ABC123"])

    def broken(_plane):
        raise RuntimeError("alerter bug")

    monkeypatch.setattr(engine.calculator, "deactivate_plane", broken)
    with caplog.at_level(logging.WARNING, logger="pyaerial"):
        assert engine.archiver.finalize(plane)
    assert "without a deactivate notification" in caplog.text
    assert _rows(config.database.path, "SELECT status FROM flights") == [("live",)]
    assert _rows(config.database.path, "SELECT alert_id, active FROM alerts") == [
        ("X1", 1)
    ]
    engine.shutdown()

    again = _engine(config)
    again.shutdown()
    assert notified == [("deactivate", "X1", "restart")]
    # Now closed, the policy discards the flight with its episode.
    assert _rows(config.database.path, "SELECT COUNT(*) FROM flights") == [(0,)]
    assert _rows(config.database.path, "SELECT COUNT(*) FROM alerts") == [(0,)]


def test_recovery_skips_a_flight_whose_alert_did_not_close(
    tmp_path, notified, monkeypatch, caplog
):
    config = _config(tmp_path, retain=False)
    config.archive = ArchiveConfig(flights="all", min_positions=100)
    plane = _pad_plane(time.time() - 600)
    _previous_run(config.database.path, plane, alert_id="A1")
    real = AlertEngine.close_restored

    def boom(self, plane, doc, **kw):
        raise RuntimeError("bug in close_restored before the record")

    monkeypatch.setattr(AlertEngine, "close_restored", boom)
    with caplog.at_level(logging.WARNING, logger="pyaerial.engine"):
        engine = _engine(config)
        engine.shutdown()
    assert "closed 1 alert" not in caplog.text
    assert "Could not close the alerts of 1 flight(s)" in caplog.text
    assert _rows(config.database.path, "SELECT status FROM flights") == [("live",)]
    assert _rows(config.database.path, "SELECT active FROM alerts") == [(1,)]
    assert _rows(
        config.database.path,
        "SELECT COUNT(*) FROM alerts WHERE flight_id NOT IN (SELECT flight_id FROM flights)",
    ) == [(0,)]

    monkeypatch.setattr(AlertEngine, "close_restored", real)
    again = _engine(config)
    again.shutdown()
    assert ("deactivate", "A1", "restart") in notified
    assert _rows(config.database.path, "SELECT COUNT(*) FROM alerts") == [(0,)]
