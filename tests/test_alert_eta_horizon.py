"""Per-zone ETA horizon: skipping unreachable zones never changes any result."""

from __future__ import annotations

import math
import random
import time

import pytest

from helpers import InlineDispatcher, make_config
from pyaerial.alerters import Alerter, register_alerter
from pyaerial.alerts import engine as engine_mod
from pyaerial.alerts.engine import AlertEngine, _rule_eta_bound
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import (
    AlertActionConfig,
    FieldConstraint,
    RuleConfig,
    TrackingConfig,
    WhileActiveConfig,
    ZoneConfig,
)
from pyaerial.constants import (
    STORE_ALT,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum

_EVENTS: dict[str, list] = {}


@register_alerter("horizon_capture")
class _Capture(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        _EVENTS.setdefault(self.arguments["engine"], []).append(
            (
                meta["type"],
                meta["zone"],
                meta["reason"]["hook"],
                meta["icao"],
                meta["eta"],
                tuple(sorted(meta["reason"]["zones"].items())),
            )
        )


def _rule(name, tag, dwell=1, **when):
    act = [AlertActionConfig(method="horizon_capture", options={"engine": tag})]
    return RuleConfig(
        name=name,
        when={k: FieldConstraint.model_validate(v) for k, v in when.items()},
        dwell_seconds=dwell,
        on_activate=act,
        on_deactivate=act,
        while_active=WhileActiveConfig(interval_seconds=2, actions=act),
    )


def _box(lat, lon, half=0.02):
    return [
        [lat - half, lon - half],
        [lat + half, lon - half],
        [lat + half, lon + half],
        [lat - half, lon + half],
        [lat - half, lon - half],
    ]


def _config(tag: str, curved: bool):
    return make_config(
        tracking=TrackingConfig(curved_projection=curved),
        zones={
            "airport": ZoneConfig(
                coordinates=_box(35.70, -78.70),
                rules=[
                    _rule("approach", tag, eta={"max": 120}, altitude={"max": 2000}),
                    _rule("imminent", tag, eta={"max": 20}),
                    # Same zone, no eta: its alert still reports the exact ETA.
                    _rule("near", tag, distance={"max": 8}),
                ],
            ),
            "range": ZoneConfig(
                coordinates=_box(35.95, -78.40, 0.05),
                # min-only: accepts large and infinite ETAs (full horizon).
                rules=[_rule("far_but_close", tag, eta={"min": 300}, dist={"max": 25})],
            ),
            "tower": ZoneConfig(
                coordinates=_box(35.60, -78.95, 0.01),
                rules=[_rule("buzz", tag, proximity={"max": 3000})],
            ),
        },
    )


def test_rule_eta_bound():
    config = _config("x", False)
    assert _rule_eta_bound(config.zones["airport"]) == 120
    assert _rule_eta_bound(config.zones["range"]) == engine_mod._ETA_HORIZON
    assert _rule_eta_bound(config.zones["tower"]) is None


@pytest.mark.parametrize("curved", [False, True])
def test_bounded_engine_matches_full_horizon_engine(monkeypatch, curved):
    _EVENTS.clear()
    clock = [0.0]
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0 + clock[0])
    monkeypatch.setattr(time, "monotonic", lambda: 10_000.0 + clock[0])
    engines = {}
    for tag in ("bounded", "full"):
        config = _config(tag, curved)
        eng = AlertEngine(config, build_polygons(config.zones))
        eng._dispatcher.close(timeout=0.0)
        eng._dispatcher = InlineDispatcher()  # type: ignore[assignment]
        engines[tag] = eng
    full = engines["full"]
    full._eta_bounds = {
        name: float(engine_mod._ETA_HORIZON) for name in full._eta_bounds
    }

    rng = random.Random(11)
    traffic = []
    for i in range(120):
        traffic.append(
            {
                "icao": f"t{i:05x}",
                "lat": 35.70 + rng.uniform(-0.6, 0.6),
                "lon": -78.70 + rng.uniform(-0.7, 0.7),
                "heading": rng.uniform(0, 360),
                "speed": rng.choice([0.0, 150.0, 450.0, 900.0]),
                "turn": rng.choice([0.0, 0.0, 1.5, -3.0]),
                "alt": rng.choice([300.0, 1500.0, 9000.0]),
            }
        )
    for i, lon in enumerate((-78.71, -78.70, -78.69)):
        # Northbound into the airport box from ~4 km south: ETA falls below 20 s.
        traffic.append(
            {
                "icao": f"in{i:04x}",
                "lat": 35.64,
                "lon": lon,
                "heading": 0.0,
                "speed": 450.0,
                "turn": 0.0,
                "alt": 300.0,
            }
        )
    # Loitering by the tower (no eta rule in that zone at all).
    traffic.append(
        {
            "icao": "loiter",
            "lat": 35.585,
            "lon": -78.95,
            "heading": 90.0,
            "speed": 60.0,
            "turn": 0.0,
            "alt": 300.0,
        }
    )
    planes = {
        tag: {
            ac["icao"]: {
                STORE_INFO: {STORE_ICAO: ac["icao"]},
                STORE_RECV_DATA: {STORE_ALT: [Datum(ac["alt"], 0.0)]},
                STORE_INTERNAL: {
                    STORE_FIRST_PACKET: 1.0,
                    STORE_MOST_RECENT_PACKET: 1.0,
                },
            }
            for ac in traffic
        }
        for tag in engines
    }
    try:
        for _tick in range(40):  # 20 s at 2 Hz
            clock[0] += 0.5
            for ac in traffic:
                # Fly straight (good enough to move aircraft across zones).
                dist_deg = ac["speed"] / 3600 * 0.5 / 111.0
                ac["lat"] += dist_deg * math.cos(math.radians(ac["heading"]))
                ac["lon"] += dist_deg * math.sin(math.radians(ac["heading"])) / 0.81
                ac["heading"] = (ac["heading"] + ac["turn"] * 0.5) % 360
                motion = ResolvedMotion(ac["heading"], ac["speed"], ac["turn"])
                for tag, eng in engines.items():
                    plane = planes[tag][ac["icao"]]
                    eng.check(plane, (ac["lat"], ac["lon"]), motion, "")
                bounded = planes["bounded"][ac["icao"]]["active_alerts"]
                reference = planes["full"][ac["icao"]]["active_alerts"]
                strip = [
                    [(a["zone"], a["rule"], a["eta"]) for a in alerts]
                    for alerts in (bounded, reference)
                ]
                assert strip[0] == strip[1], ac["icao"]
    finally:
        for eng in engines.values():
            eng.close()
    assert _EVENTS["bounded"] == _EVENTS["full"]
    # The scenario exercises every rule, not just the trivial "nothing fires".
    fired = {(e[0], e[2]) for e in _EVENTS["full"]}
    for rule in ("approach", "imminent", "near", "far_but_close", "buzz"):
        assert (rule, "activate") in fired, rule
