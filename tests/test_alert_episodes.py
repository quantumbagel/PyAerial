"""Alert episodes: every activate gets exactly one deactivate, across engine
restarts and crashes, ICAO reuse, orphan sweeps and flight handovers."""

from __future__ import annotations

import json
import logging
import sqlite3
import time

import pytest

from helpers import (
    LOCAL_REDIS_URI,
    InlineDispatcher,
    alert_doc,
    fine_track_plane,
    make_config,
    make_rule,
    pad_config,
)
from pyaerial.alerters import Alerter, register_alerter
from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import AlertActionConfig, WhileActiveConfig, ZoneConfig
from pyaerial.constants import (
    STORE_ALT,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.engine import Engine
from pyaerial.models import Datum, flight_id_for_plane
from pyaerial.pipeline import PlaneCalculator
from pyaerial.store.redis_live import LiveUnavailable, RedisLiveStore

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


def _restart_plane(last: float | None = None, first: float = 1_700_000_000.0) -> dict:
    last = time.time() if last is None else last
    return {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: first,
            STORE_MOST_RECENT_PACKET: last,
            "total_packets": 2,
            "packet_type": {},
        },
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(_INSIDE[0], last - 5), Datum(_INSIDE[0] + 0.0001, last)],
            STORE_LONG: [Datum(_INSIDE[1], last - 5), Datum(_INSIDE[1], last)],
            STORE_ALT: [Datum(400.0, last)],
        },
    }


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
        engine.deactivate(_restart_plane(last=1_700_000_160.0))
        engine.deactivate(_restart_plane(last=1_700_000_160.0))  # orphan sweep runs again
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
            engine.deactivate(_restart_plane(last=1_700_000_160.0))
        _drain(engine)
    finally:
        engine.close()
    assert _RESTART_EVENTS == []
    assert store.records[0]["active"] is False
    assert "no longer configured" in caplog.text


def _activate(engine: AlertEngine, plane: dict, monkeypatch) -> None:
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
        plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET] = time.time() - age
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


def _run_until_active(engine: Engine, monkeypatch) -> dict:
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
# Alert state belongs to a flight: ICAO reuse, orphan sweeps and segment
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


def _identity_plane(first: float, last: float | None = None) -> dict:
    last = time.time() if last is None else last
    return {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_INTERNAL: {STORE_FIRST_PACKET: first, STORE_MOST_RECENT_PACKET: last},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(_INSIDE[0], last - 5), Datum(_INSIDE[0] + 0.0001, last)],
            STORE_LONG: [Datum(_INSIDE[1], last - 5), Datum(_INSIDE[1], last)],
            STORE_ALT: [Datum(400.0, last)],
        },
    }


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


# --- ICAO collision: orphan sweep archives an old live copy ------------------


def test_orphan_of_old_flight_leaves_live_flight_alert_alone(clock):
    _IDENTITY_EVENTS.clear()
    store = RedisLiveStore("redis://localhost:1/0", memory_only=True, writer=True)
    engine = _alert_engine(_identity_config(), store)
    try:
        live = _identity_plane(first=time.time() - 30)
        for _ in range(3):
            _tick(engine, live, clock)
        (state,) = engine._alert_state.values()
        orphan = _identity_plane(first=1_700_000_000.0, last=1_700_000_500.0)
        assert engine.deactivate(orphan) is False
        assert list(engine._alert_state.values()) == [state]
        assert live["active_alerts"]  # the live plane's list is untouched
        for _ in range(3):
            _tick(engine, live, clock)
        live_alerts = store.get_alerts(flight_id=flight_id_for_plane(live))
        assert [a["alert_id"] for a in live_alerts] == [state["alert_id"]]
    finally:
        engine.close()
        store.close()
    assert [e["hook"] for e in _IDENTITY_EVENTS] == ["activate"]  # no lost, no re-activate


def test_orphan_still_closes_its_own_leftover_episode(clock):
    _IDENTITY_EVENTS.clear()
    store = RedisLiveStore("redis://localhost:1/0", memory_only=True, writer=True)
    engine = _alert_engine(_identity_config(), store)
    try:
        orphan = _identity_plane(first=1_700_000_000.0, last=1_700_000_500.0)
        # Left active by a previous engine process.
        engine.check(orphan, _INSIDE, _MOTION, "N1")
        clock[0] += 2
        engine.check(orphan, _INSIDE, _MOTION, "N1")
        old_id = next(iter(engine._alert_state.values()))["alert_id"]
        engine._alert_state.clear()
        engine._live_flight.clear()
        _IDENTITY_EVENTS.clear()
        live = _identity_plane(first=time.time() - 30)
        _tick(engine, live, clock)
        assert engine.deactivate(orphan) is False
    finally:
        engine.close()
        store.close()
    assert [(e["hook"], e["alert_id"], e["ended_by"]) for e in _IDENTITY_EVENTS] == [
        ("deactivate", old_id, "restart")
    ]


def test_calculator_keeps_motion_and_lookups_for_live_flight(clock):
    config = _identity_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        live = _identity_plane(first=time.time() - 30)
        calc.calculate_plane(live)
        clock[0] += 2
        calc.calculate_plane(live)
        assert "abc123" in calc._kalman_filters
        assert calc._pending_match or calc._alert_state
        calc._lookup_failures["abc123"] = 2
        calc.deactivate_plane(_identity_plane(first=1_700_000_000.0, last=1_700_000_500.0))
        assert "abc123" in calc._kalman_filters
        assert calc._pending_match or calc._alert_state
        assert calc._lookup_failures["abc123"] == 2
        # The live flight's own expiry still clears everything.
        calc.deactivate_plane(live)
        assert "abc123" not in calc._kalman_filters
        assert not calc._pending_match and not calc._alert_state
        assert "abc123" not in calc._lookup_failures
    finally:
        calc.close()


def test_pending_match_survives_orphan_sweep(clock):
    config = _identity_config()
    config.zones["pad"].rules[0].dwell_seconds = 5
    engine = _alert_engine(config)
    try:
        live = _identity_plane(first=time.time() - 30)
        _tick(engine, live, clock, seconds=3)  # t=0: first match
        _tick(engine, live, clock, seconds=3)  # t=3
        engine.deactivate(_identity_plane(first=1_700_000_000.0, last=1_700_000_500.0))
        _tick(engine, live, clock)  # t=6
        # Dwell measured from t=0, not restarted by the sweep at t=3.
        assert engine._alert_state
    finally:
        engine.close()


# --- segmentation handover ---------------------------------------------------


def _segmented_run(clock, *, explicit: bool):
    _IDENTITY_EVENTS.clear()
    store = RedisLiveStore("redis://localhost:1/0", memory_only=True, writer=True)
    engine = _alert_engine(_identity_config(while_active=4), store)
    t0 = time.time() - 600
    plane = _identity_plane(first=t0)
    old_id = flight_id_for_plane(plane)
    seen: dict = {}
    try:
        for _ in range(4):  # activate at t=1, while_active at t=5
            _tick(engine, plane, clock)
        (state,) = engine._alert_state.values()
        seen["episode"], seen["activated_at"] = state["alert_id"], state["activated_at"]
        # max_flight_seconds reached: the same aircraft continues as a new flight.
        plane[STORE_INTERNAL][STORE_FIRST_PACKET] = t0 + 300
        new_id = flight_id_for_plane(plane)
        if explicit:
            engine.handover(plane, old_id, new_id)
            engine.handover(plane, old_id, new_id)  # idempotent
            store.pop_flight(old_id)  # the engine archives and pops segment 1
        _tick(engine, plane, clock)
        if not explicit:
            store.pop_flight(old_id)
        seen["active_mid"] = store.get_alerts(active_only=True)
        for _ in range(3):
            _tick(engine, plane, clock)
        _tick(engine, plane, clock, position=_OUTSIDE)
        return store, seen, new_id
    finally:
        engine.close()


@pytest.mark.parametrize("explicit", [True, False])
def test_handover_continues_episode_without_notifications(clock, explicit):
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


def test_handover_keeps_while_active_cadence(clock):
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
        engine.handover({STORE_INFO: {}}, "x-1", "x-2")
        _tick(engine, plane, clock)
    finally:
        engine.close()


# --- Active alerts whose flight never reached live:flights -------------------


def _alerting_config(tmp_path):
    rule = make_rule(name="warn", retain=True, dwell_seconds=1)
    rule.on_deactivate = [AlertActionConfig(method="print")]
    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[rule],
            )
        }
    )
    config.database.path = str(tmp_path / "pyaerial.db")
    return config


def _stray_alert(alert_id: str, flight_id: str, icao: str) -> str:
    return json.dumps(
        {
            "alert_id": alert_id,
            "flight_id": flight_id,
            "icao": icao,
            "callsign": "SWA1",
            "zone": "pad",
            "rule": "warn",
            "active": True,
            "activated_at": 1_700_000_010.0,
            "deactivated_at": None,
            "last_updated": 1_700_000_020.0,
            "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
        }
    )


def test_startup_closes_active_alerts_of_flights_that_never_went_live(
    fake, tmp_path, monkeypatch
):
    fid = "abc123-1700000000"
    fake.hashes["live:active_alerts"] = {"a1": _stray_alert("a1", fid, "abc123")}
    fake.hashes["live:alert_episodes"] = {"a1": _stray_alert("a1", fid, "abc123")}
    fake.hashes[f"live:alerts:{fid}"] = {"a1": _stray_alert("a1", fid, "abc123")}
    runs: list[dict] = []
    real_run = AlertEngine._run_actions

    def record(self, actions, meta, payload, alert_id):
        runs.append({"meta": meta, "alert_id": alert_id})
        return real_run(self, actions, meta, payload, alert_id)

    monkeypatch.setattr(AlertEngine, "_run_actions", record)
    engine = Engine(
        _alerting_config(tmp_path), aircraft_db_path=str(tmp_path / "aircraft.db")
    )
    try:
        # The deactivate notification went out, attributed to the restart.
        assert [r["alert_id"] for r in runs] == ["a1"]
        assert "restart" in json.dumps(runs[0]["meta"], default=str)
        # Closed in Redis: no ghost active alert, no leftover per-flight keys.
        assert "a1" not in fake.hashes.get("live:active_alerts", {})
        assert f"live:alerts:{fid}" not in fake.hashes
        # The completed episode is archived with its flight.
        archived = engine.history_store.get_alerts(flight_id=fid)
        assert [a["alert_id"] for a in archived] == ["a1"]
        assert archived[0]["active"] is False and archived[0]["deactivated_at"]
    finally:
        engine.shutdown()


def test_stray_active_alerts_skip_flights_still_live(fake):
    fake.sets["live:flights"] = {"live01-1"}
    fake.hashes["live:active_alerts"] = {
        "a1": _stray_alert("a1", "live01-1", "live01"),
        "a2": _stray_alert("a2", "gone02-2", "gone02"),
        "a3": "{not json",
    }
    store = RedisLiveStore(LOCAL_REDIS_URI, writer=True)
    strays = store.stray_active_alerts()
    assert [doc["alert_id"] for doc in strays] == ["a2"]
    # Unusable entries cannot be closed properly; they are dropped.
    assert "a3" not in fake.hashes["live:active_alerts"]
    assert "a1" in fake.hashes["live:active_alerts"]


# --- Leftover flights on restart ---------------------------------------------


def test_restart_archives_every_episode_of_a_leftover_flight(fake, tmp_path):
    t = time.time() - 300
    fid = f"abc123-{int(t)}"
    fake.sets["live:flights"] = {fid}
    fake.kv[f"live:flight:{fid}"] = json.dumps(
        {
            "flight_id": fid,
            "icao": "abc123",
            "start_time": t,
            "end_time": t + 60,
            "info": {},
        }
    )
    fake.zsets[f"live:telemetry:{fid}"] = {
        json.dumps({"timestamp": t + n, "latitude": 35.725, "longitude": -78.695}): t
        + n
        for n in range(3)
    }
    fake.hashes[f"live:alerts:{fid}"] = {
        "earlier": alert_doc("earlier", fid, active=False, at=t + 5),
        "current": alert_doc("current", fid, active=True, at=t + 30),
    }
    fake.hashes["live:active_alerts"] = {
        "current": alert_doc("current", fid, active=True, at=t + 30)
    }
    engine = Engine(pad_config(tmp_path), aircraft_db_path=str(tmp_path / "a.db"))
    try:
        archived = {
            a["alert_id"]: a for a in engine.history_store.get_alerts(flight_id=fid)
        }
        assert set(archived) == {"earlier", "current"}
        assert archived["current"]["active"] is False
    finally:
        engine.shutdown()


def test_persist_defers_when_alerts_cannot_be_read(fake, tmp_path, monkeypatch):
    engine = Engine(pad_config(tmp_path), aircraft_db_path=str(tmp_path / "a.db"))
    try:
        fid = "abc123-1700000000"

        def unreadable(**_kwargs):
            raise LiveUnavailable("live store is unavailable")

        monkeypatch.setattr(engine.live_store, "get_alerts", unreadable)
        assert (
            engine._persist(fid, fine_track_plane("abc123", 1_700_000_000.0), from_snapshot=True)
            is False
        )
        assert engine.history_store.get_flight(fid) is None
    finally:
        monkeypatch.undo()
        engine.shutdown()
