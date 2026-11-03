"""Alert episodes: every activate gets exactly one deactivate, across engine
restarts and crashes, and flight handovers."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time

import pytest

from helpers import (
    LOCAL_REDIS_URI,
    episode_recorder,
    InlineDispatcher,
    fine_track_plane,
    make_config,
    pad_config,
)
from pyaerial.alerters import Alerter, register_alerter
from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import AlertActionConfig, WhileActiveConfig
from pyaerial.constants import (
    STORE_ALT,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.engine import Engine
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState
from pyaerial.store.archive import ArchiveReader, HistoryUnavailable
from pyaerial.store.live import LiveWriter

# --- Engine restarts and crashes ---------------------------------------------
# Episodes left active by a crashed or stopped engine are closed exactly once.


_RESTART_EVENTS: list[tuple[str, str, str | None]] = []


@register_alerter("restart_capture")
class _RestartCapture(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        reason = meta["reason"]
        _RESTART_EVENTS.append((reason["hook"], meta["alert_id"], reason.get("ended_by")))


_RESTART_ACTION = AlertActionConfig(method="restart_capture")


_INSIDE = (35.725, -78.695)


_REAL_MONOTONIC = time.monotonic


def _restart_config(tmp_path=None):
    config = make_config()
    if tmp_path is not None:
        config.database.path = str(tmp_path / "history.db")
    rule = config.zones["pad"].rules[0]
    rule.dwell_seconds = 1
    rule.on_activate = [_RESTART_ACTION]
    rule.on_deactivate = [_RESTART_ACTION]
    return config


def _restart_plane(last: float | None = None, first: float = 1_700_000_000.0) -> Plane:
    last = time.time() if last is None else last
    return Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=first, last_update=last, packets=2),
        received_data={
            STORE_LAT: [Datum(_INSIDE[0], last - 5), Datum(_INSIDE[0] + 0.0001, last)],
            STORE_LONG: [Datum(_INSIDE[1], last - 5), Datum(_INSIDE[1], last)],
            STORE_ALT: [Datum(400.0, last)],
        },
    )


class _RestartStore:
    """Live-store double holding episodes by alert_id."""

    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs = {d["alert_id"]: d for d in docs or []}
        self.records: list[dict] = []

    def get_alerts(self, *, flight_id=None, active_only=True, **_kw):
        return [
            d
            for d in self.docs.values()
            if (not flight_id or d["flight_id"] == flight_id)
            and (d["active"] or not active_only)
        ]

    def record_alert_episode(self, plane, meta, payload, **kw) -> None:
        self.records.append({"meta": meta, **kw})
        doc = self.docs.setdefault(kw["alert_id"], {"alert_id": kw["alert_id"]})
        doc.update(flight_id="abc123-1700000000", active=kw["active"])

    def update_active_alert(self, *args, **kwargs) -> None:
        pass


def _orphan(**extra) -> dict:
    return {
        "alert_id": "abc123-1700000000:pad:warn:old",
        "flight_id": "abc123-1700000000",
        "icao": "abc123",
        "callsign": "N1",
        "zone": "pad",
        "rule": "warn",
        "active": True,
        "activated_at": 1_700_000_100.0,
        "last_updated": 1_700_000_160.0,
        "eta": 0.0,
        **extra,
    }


def _drain(engine: AlertEngine) -> None:
    engine._dispatcher.close(timeout=5.0)


def test_alert_left_active_by_crashed_engine_is_deactivated_once():
    _RESTART_EVENTS.clear()
    store = _RestartStore([_orphan()])
    engine = AlertEngine(_restart_config(), build_polygons(_restart_config().zones), store)  # type: ignore[arg-type]
    try:
        engine.close_restored(_restart_plane(last=1_700_000_160.0), _orphan())
        _drain(engine)
    finally:
        engine.close()
    assert _RESTART_EVENTS == [("deactivate", "abc123-1700000000:pad:warn:old", "restart")]
    (record,) = store.records
    assert record["active"] is False
    assert record["activated_at"] == 1_700_000_100.0
    # Closed at the last time the old engine saw it hold, not at restart time.
    assert record["deactivated_at"] == 1_700_000_160.0


def test_orphan_of_removed_rule_is_closed_without_notification(caplog):
    _RESTART_EVENTS.clear()
    store = _RestartStore([_orphan(rule="gone")])
    config = _restart_config()
    engine = AlertEngine(config, build_polygons(config.zones), store)  # type: ignore[arg-type]
    try:
        with caplog.at_level(logging.WARNING, logger="pyaerial.alerts"):
            engine.close_restored(
                _restart_plane(last=1_700_000_160.0), _orphan(rule="gone")
            )
        _drain(engine)
    finally:
        engine.close()
    assert _RESTART_EVENTS == []
    assert store.records[0]["active"] is False
    assert "no longer configured" in caplog.text


def _activate(engine: AlertEngine, plane: Plane, monkeypatch) -> None:
    mono = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    motion = ResolvedMotion(heading_deg=0.0, speed_kph=0.0, turn_rate_deg_s=0.0)
    for _ in range(3):
        engine.check(plane, _INSIDE, motion, "N1")
        mono[0] += 1.0
    monkeypatch.setattr(time, "monotonic", _REAL_MONOTONIC)


@pytest.mark.parametrize(
    ("age", "expected"),
    [(5.0, "shutdown"), (10_000.0, "lost")],
)
def test_deactivate_reports_why_the_episode_ended(monkeypatch, age, expected):
    _RESTART_EVENTS.clear()
    config = _restart_config()
    engine = AlertEngine(config, build_polygons(config.zones))
    try:
        plane = _restart_plane()
        _activate(engine, plane, monkeypatch)
        plane.track.last_update = time.time() - age
        engine.deactivate(plane)
        _drain(engine)
    finally:
        engine.close()
    assert [e[0] for e in _RESTART_EVENTS] == ["activate", "deactivate"]
    assert _RESTART_EVENTS[1][2] == expected
    assert _RESTART_EVENTS[0][1] == _RESTART_EVENTS[1][1]


def test_conditions_clearing_reports_cleared(monkeypatch):
    _RESTART_EVENTS.clear()
    config = _restart_config()
    engine = AlertEngine(config, build_polygons(config.zones))
    try:
        plane = _restart_plane()
        _activate(engine, plane, monkeypatch)
        motion = ResolvedMotion(heading_deg=0.0, speed_kph=0.0, turn_rate_deg_s=0.0)
        engine.check(plane, (40.0, -70.0), motion, "N1")
        _drain(engine)
    finally:
        engine.close()
    assert _RESTART_EVENTS[-1][0] == "deactivate" and _RESTART_EVENTS[-1][2] == "cleared"


def _restart_engine(tmp_path) -> Engine:
    return Engine(_restart_config(tmp_path), aircraft_db_path=str(tmp_path / "aircraft.db"))


def _run_until_active(engine: Engine, monkeypatch) -> Plane:
    plane = _restart_plane(first=time.time() - 5)
    engine.tracker.planes["abc123"] = plane
    real = time.monotonic
    offset = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])
    for _ in range(3):
        engine.calculator.calculate_all(engine.tracker.planes)
        engine.live_store.write_live_planes(engine.tracker.planes)
        offset[0] += 1.0
    monkeypatch.setattr(time, "monotonic", real)
    return plane


def _wait_for_events(count: int) -> None:
    deadline = time.monotonic() + 5.0
    while len(_RESTART_EVENTS) < count and time.monotonic() < deadline:
        time.sleep(0.01)


def test_crash_then_restart_deactivates_and_clears_live_state(
    fake, tmp_path, monkeypatch
):
    _RESTART_EVENTS.clear()
    first = _restart_engine(tmp_path)
    _run_until_active(first, monkeypatch)
    _wait_for_events(1)
    assert [e[0] for e in _RESTART_EVENTS] == ["activate"]
    assert fake.hgetall("live:active_alerts")
    # Crash: no shutdown(); the heartbeat key expires on its TTL.
    first.calculator.alerts._dispatcher.close(timeout=1.0)
    fake.delete("live:engine")
    with sqlite3.connect(tmp_path / "history.db") as conn:  # its lease expires too
        conn.execute("UPDATE engine_lease SET renewed_at = renewed_at - 60")
    conn.close()

    second = _restart_engine(tmp_path)
    try:
        _wait_for_events(2)
        assert [e[:1] + e[2:] for e in _RESTART_EVENTS] == [
            ("activate", None),
            ("deactivate", "restart"),
        ]
        assert _RESTART_EVENTS[0][1] == _RESTART_EVENTS[1][1]
        assert fake.hgetall("live:active_alerts") == {}
        assert second.live_store.get_alerts(active_only=True) == []
    finally:
        second.shutdown()
    rows = (
        sqlite3.connect(tmp_path / "history.db")
        .execute("SELECT alert_id, deactivated_at FROM alerts")
        .fetchall()
    )
    assert [r[0] for r in rows] == [_RESTART_EVENTS[0][1]]
    assert rows[0][1] is not None
    assert len(_RESTART_EVENTS) == 2  # second engine's shutdown adds nothing


def test_graceful_shutdown_deactivates_once_and_restart_adds_nothing(
    fake, tmp_path, monkeypatch
):
    _RESTART_EVENTS.clear()
    first = _restart_engine(tmp_path)
    _run_until_active(first, monkeypatch)
    first.shutdown()
    assert [e[0] for e in _RESTART_EVENTS] == ["activate", "deactivate"]
    assert _RESTART_EVENTS[1][2] == "shutdown"
    assert fake.hgetall("live:active_alerts") == {}

    second = _restart_engine(tmp_path)
    second.shutdown()
    assert len(_RESTART_EVENTS) == 2


# --- Flight identity ---------------------------------------------------------
# Alert state belongs to a flight: segment
# handovers.


_IDENTITY_EVENTS: list[dict] = []


_OUTSIDE = (40.0, -70.0)


_MOTION = ResolvedMotion(heading_deg=0.0, speed_kph=0.0, turn_rate_deg_s=0.0)


@register_alerter("identity_capture")
class _IdentityCapture(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        _IDENTITY_EVENTS.append(
            {
                "hook": meta["reason"]["hook"],
                "alert_id": meta["alert_id"],
                "flight_id": meta["flight_id"],
                "ended_by": meta["reason"].get("ended_by"),
            }
        )


def _identity_config(while_active: int | None = None):
    config = make_config()
    rule = config.zones["pad"].rules[0]
    rule.dwell_seconds = 1
    act = [AlertActionConfig(method="identity_capture")]
    rule.on_activate = act
    rule.on_deactivate = act
    if while_active:
        rule.while_active = WhileActiveConfig(
            interval_seconds=while_active, actions=act
        )
    return config


def _identity_plane(first: float, last: float | None = None) -> Plane:
    last = time.time() if last is None else last
    return Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=first, last_update=last),
        received_data={
            STORE_LAT: [Datum(_INSIDE[0], last - 5), Datum(_INSIDE[0] + 0.0001, last)],
            STORE_LONG: [Datum(_INSIDE[1], last - 5), Datum(_INSIDE[1], last)],
            STORE_ALT: [Datum(400.0, last)],
        },
    )


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: 5_000.0 + now[0])
    return now


def _alert_engine(config, store=None) -> AlertEngine:
    engine = AlertEngine(config, build_polygons(config.zones), store)
    engine._dispatcher.close(timeout=0.0)
    engine._dispatcher = InlineDispatcher()  # type: ignore[assignment]
    return engine


def _tick(engine, plane, clock, position=_INSIDE, seconds=1.0):
    engine.check(plane, position, _MOTION, "N1")
    clock[0] += seconds


# --- segmentation handover ---------------------------------------------------


def _segmented_run(clock, *, explicit: bool):
    _IDENTITY_EVENTS.clear()
    store = LiveWriter(LOCAL_REDIS_URI)
    engine = _alert_engine(_identity_config(while_active=4), episode_recorder(store))
    t0 = time.time() - 600
    plane = _identity_plane(first=t0)
    old_id = plane.flight_id
    seen: dict = {}
    try:
        for _ in range(4):  # activate at t=1, while_active at t=5
            _tick(engine, plane, clock)
        (state,) = engine._alert_state.values()
        seen["episode"], seen["activated_at"] = state["alert_id"], state["activated_at"]
        # max_flight_seconds reached: the same aircraft continues as a new flight.
        plane.track.first_packet = t0 + 300
        new_id = plane.flight_id
        if explicit:
            engine.handover(plane, old_id, new_id)
            engine.handover(plane, old_id, new_id)  # idempotent
        _tick(engine, plane, clock)
        store.write_live_planes({"ABC123": plane})  # segment 1 drops out
        seen["active_mid"] = store.get_alerts(active_only=True)
        for _ in range(3):
            _tick(engine, plane, clock)
        _tick(engine, plane, clock, position=_OUTSIDE)
        store.write_live_planes({"ABC123": plane})
        return store, seen, new_id
    finally:
        engine.close()


@pytest.mark.parametrize("explicit", [True, False])
def test_handover_continues_episode_without_notifications(fake, clock, explicit):
    store, seen, new_id = _segmented_run(clock, explicit=explicit)
    episode = seen["episode"]
    try:
        hooks = [e["hook"] for e in _IDENTITY_EVENTS]
        assert hooks[0] == "activate" and hooks[-1] == "deactivate"
        assert hooks.count("activate") == 1 and hooks.count("deactivate") == 1
        assert "while_active" in hooks
        # Consumers pair everything by one id, across the segment boundary.
        assert {e["alert_id"] for e in _IDENTITY_EVENTS} == {episode}
        assert _IDENTITY_EVENTS[-1]["flight_id"] == new_id
        assert _IDENTITY_EVENTS[-1]["ended_by"] == "cleared"
        # Live: still active after segment 1 was popped, now on the new flight.
        assert [
            (a["alert_id"], a["flight_id"], a["activated_at"]) for a in seen["active_mid"]
        ] == [(episode, new_id, seen["activated_at"])]
        (closed,) = store.get_alerts(flight_id=new_id, active_only=False)
        assert closed["alert_id"] == episode and closed["active"] is False
        assert closed["activated_at"] == seen["activated_at"]
        assert store.get_alerts(active_only=True) == []
    finally:
        store.close()


def test_handover_keeps_while_active_cadence(fake, clock):
    _segmented_run(clock, explicit=True)
    # 1 s ticks: activate at t=1, handover before t=4, while_active due at
    # t=5. Resetting the cadence at the handover would push it to t=8, the
    # tick on which the aircraft leaves, so none would fire.
    whiles = [i for i, e in enumerate(_IDENTITY_EVENTS) if e["hook"] == "while_active"]
    assert whiles == [1]


def test_handover_without_alerts_or_store_is_harmless(clock):
    engine = _alert_engine(_identity_config())
    try:
        plane = _identity_plane(first=time.time() - 30)
        engine.handover(plane, "abc123-1", "abc123-2")
        engine.handover(
            Plane(info=AircraftInfo(icao=""), track=TrackState(1.0, 1.0)), "x-1", "x-2"
        )
        _tick(engine, plane, clock)
    finally:
        engine.close()


# --- Finalize reads the flight's episodes from the archive -------------------


def test_persist_defers_when_alerts_cannot_be_read(fake, tmp_path, monkeypatch):
    engine = Engine(pad_config(tmp_path), aircraft_db_path=str(tmp_path / "a.db"))
    try:
        fid = "abc123-1700000000"

        def unreadable(_flight_id):
            raise HistoryUnavailable("history archive is unavailable")

        monkeypatch.setattr(engine.history_store, "flight_alerts", unreadable)
        assert (
            engine.archiver.finalize(fine_track_plane("abc123", 1_700_000_000.0))
            is False
        )
        assert fid in engine.archiver.pending
        with contextlib.closing(ArchiveReader(engine.history_store.path)) as archive:
            assert archive.get_flight(fid) is None
    finally:
        monkeypatch.undo()
        engine.shutdown()
