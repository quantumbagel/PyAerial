"""End to end: a synthetic flight through AlertEngine with a mixed rule set.

The aircraft approaches a zone from the south, crosses it, hovers on the
north boundary (alternating 11 m inside / 22 m outside every tick), then
leaves. Expected notifications, derived from the rule semantics:

* ``approach`` (eta <= 60 s, altitude, integrity >= 0.8; dwell 2 s, no
  hysteresis): activates ~2 s after ETA drops to 60 s, while_active every
  10 s, deactivates on the first outside hover tick (heading away: no ETA)
  and never re-activates during the hover (no 2 s continuous match).
* ``inside`` (distance 0, altitude; hysteresis 3 s): activates 3 s after
  entering, rides out the hover without flapping, while_active every 4 s
  while matched, deactivates 3 s after leaving for good.
* ``near`` (proximity <= 500 m; dwell 1 s): one activation, one deactivation
  once the aircraft is more than 500 m out.
* ``spoof`` (integrity <= 0.5): never fires for an aircraft scoring 0.95."""

from __future__ import annotations

import time

import pytest

from helpers import InlineDispatcher, make_config
from pyaerial.alerters import Alerter, register_alerter
from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import (
    AlertActionConfig,
    FieldConstraint,
    RuleConfig,
    WhileActiveConfig,
    ZoneConfig,
)
from pyaerial.constants import (
    STORE_ALT,
)
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState

_EVENTS: list[dict] = []


_NOW = [0.0]


@register_alerter("scenario_capture")
class _Capture(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        _EVENTS.append(
            {
                "t": _NOW[0],
                "rule": meta["type"],
                "hook": meta["reason"]["hook"],
                "ended_by": meta["reason"].get("ended_by"),
                "alert_id": meta["alert_id"],
            }
        )


_ACT = [AlertActionConfig(method="scenario_capture")]


_TICK = 0.5


_SPEED_KPH = 180.0


_DEG_PER_S = (_SPEED_KPH / 3.6) / 111_000.0  # northbound, degrees latitude/s


_SOUTH, _NORTH = 35.72, 35.73


def _rule(name: str, when: dict, *, dwell: int, hysteresis: float = 0, every=None):
    return RuleConfig(
        name=name,
        when={k: FieldConstraint.model_validate(v) for k, v in when.items()},
        dwell_seconds=dwell,
        hysteresis_seconds=hysteresis,
        on_activate=_ACT,
        on_deactivate=_ACT,
        while_active=(
            WhileActiveConfig(interval_seconds=every, actions=_ACT) if every else None
        ),
    )


def _config():
    return make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [_SOUTH, -78.70],
                    [_NORTH, -78.70],
                    [_NORTH, -78.69],
                    [_SOUTH, -78.69],
                    [_SOUTH, -78.70],
                ],
                rules=[
                    _rule(
                        "approach",
                        {
                            "eta": {"max": 60},
                            "altitude": {"max": 1500},
                            "integrity": {"min": 0.8},
                        },
                        dwell=2,
                        every=10,
                    ),
                    _rule(
                        "inside",
                        {"distance": {"max": 0}, "alt": {"max": 1000}},
                        dwell=60,  # overridden by hysteresis_seconds
                        hysteresis=3,
                        every=4,
                    ),
                    _rule("near", {"proximity": {"max": 500}}, dwell=1),
                    _rule(
                        "spoof",
                        {"integrity": {"max": 0.5}, "distance": {"max": 5}},
                        dwell=1,
                    ),
                ],
            )
        }
    )


def _track():
    """Yield ``(t, lat)`` every tick: approach, cross, hover, leave."""
    t = 0.0
    lat = _SOUTH - 0.045  # ~5 km south: ETA ~100 s
    while lat < 35.725:
        yield t, lat
        t += _TICK
        lat += _DEG_PER_S * _TICK
    hover_start = t
    for i in range(20):  # 10 s on the north boundary
        yield t, (_NORTH - 0.0001) if i % 2 == 0 else (_NORTH + 0.0002)
        t += _TICK
    lat = _NORTH + 0.0002
    leave_start = t
    for _ in range(60):  # 30 s northbound, ~1.5 km
        yield t, lat
        t += _TICK
        lat += _DEG_PER_S * _TICK
    _track.marks = (hover_start, leave_start)  # type: ignore[attr-defined]


@pytest.fixture
def events(monkeypatch):
    _EVENTS.clear()
    t0 = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: t0 + _NOW[0])
    monkeypatch.setattr(time, "monotonic", lambda: 50_000.0 + _NOW[0])
    config = _config()
    engine = AlertEngine(config, build_polygons(config.zones))
    engine._dispatcher.close(timeout=0.0)
    engine._dispatcher = InlineDispatcher()  # type: ignore[assignment]
    plane = Plane(
        info=AircraftInfo(
            icao="abc123", integrity={"score": 0.95, "status": "ok", "checks": {}}
        ),
        track=TrackState(first_packet=t0, last_update=t0),
        received_data={STORE_ALT: [Datum(800.0, t0)]},
    )
    motion = ResolvedMotion(heading_deg=0.0, speed_kph=_SPEED_KPH, turn_rate_deg_s=0.0)
    try:
        for t, lat in _track():
            _NOW[0] = t
            engine.check(plane, (lat, -78.695), motion, "N123AB")
    finally:
        engine.close()
    return list(_EVENTS)


def _for(events: list[dict], rule: str) -> list[dict]:
    return [e for e in events if e["rule"] == rule]


def _gaps(events: list[dict]) -> list[float]:
    times = [e["t"] for e in events]
    return [b - a for a, b in zip(times, times[1:], strict=False)]


def test_each_rule_activates_once_and_deactivates_once(events):
    for rule in ("approach", "inside", "near"):
        hooks = [e["hook"] for e in _for(events, rule)]
        assert hooks[0] == "activate", rule
        assert hooks[-1] == "deactivate", rule
        assert hooks.count("activate") == 1, rule
        assert hooks.count("deactivate") == 1, rule
        assert set(hooks[1:-1]) <= {"while_active"}, rule
        assert len({e["alert_id"] for e in _for(events, rule)}) == 1, rule
        assert _for(events, rule)[-1]["ended_by"] == "cleared", rule
    assert _for(events, "spoof") == []


def test_activation_and_deactivation_timing(events):
    hover_start, leave_start = _track.marks  # type: ignore[attr-defined]
    enter = (_SOUTH - (_SOUTH - 0.045)) / _DEG_PER_S  # ~100 s
    approach = _for(events, "approach")
    # ETA <= 60 s at about enter - 60, plus the 2 s dwell (geodesic vs the
    # flat 111 km/deg used to move the aircraft: allow a couple of seconds).
    assert abs(approach[0]["t"] - (enter - 60 + 2)) <= 3
    # First outside hover tick: heading away from the zone, no ETA.
    assert approach[-1]["t"] == hover_start + _TICK

    inside = _for(events, "inside")
    assert abs(inside[0]["t"] - (enter + 3)) <= 1  # hysteresis is the hold
    # Off-delay counts from the first unmatched tick of the final exit.
    assert inside[-1]["t"] == hover_start + 19 * _TICK + 3.0

    near = _for(events, "near")
    assert near[-1]["t"] > leave_start  # still within 500 m while hovering


def test_while_active_cadence(events):
    approach = [e for e in _for(events, "approach") if e["hook"] != "deactivate"]
    assert len(approach) >= 6  # ~60 s active before the hover
    assert all(gap == 10.0 for gap in _gaps(approach))

    inside = [e for e in _for(events, "inside") if e["hook"] != "deactivate"]
    assert len(inside) >= 4
    # Every 4 s while matched; on the boundary it may slip to the next
    # matching (inside) tick, never earlier and never more than one tick late.
    assert all(4.0 <= gap <= 4.0 + _TICK for gap in _gaps(inside))

    assert [e["hook"] for e in _for(events, "near")] == ["activate", "deactivate"]
