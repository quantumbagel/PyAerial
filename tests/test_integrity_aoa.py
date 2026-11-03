"""Angle-of-arrival integrity checks: learned array bias and bearing clusters."""

from __future__ import annotations

import pytest

from integrity_world import (
    HOME,
    Aircraft,
    Antenna,
    World,
    integrity_config,
    integrity_of,
    make_monitor,
    run_world,
)
from pyaerial.config.schema import (
    ReceiverConfig,
    ReceiverPositionConfig,
)
from pyaerial.integrity import STATUS_OK, geodesy

# --- Bearing clusters and learned AoA bias -----------------------------------


def _spoof_from(
    world: World, icao: str, emitter_bearing: float, claim_km: float
) -> Aircraft:
    lat, lon = geodesy.destination(HOME[0], HOME[1], 45, claim_km * 1000)
    elat, elon = geodesy.destination(HOME[0], HOME[1], emitter_bearing, 5_000)
    return world.add(
        Aircraft(
            icao,
            lat,
            lon,
            alt=9000,
            speed_kph=700,
            track=270,
            emitter=(elat, elon, 50.0),
        )
    )


def test_bearing_cluster_spans_north():
    antennas = [Antenna("k", *HOME)]
    world = World(antennas, seed=5)
    world.add_genuine(10)
    _spoof_from(world, "b10001", 358.5, 100)
    _spoof_from(world, "b10002", 1.5, 140)
    monitor = make_monitor(world, integrity_config(antennas, kind="aoa"))
    run_world(world, monitor, 60)
    for icao in ("b10001", "b10002"):
        doc = integrity_of(world, icao)
        assert doc["checks"]["aoa"]["status"] == "fail"
        assert doc["checks"]["emitter_cluster"]["status"] == "fail", doc["checks"]


@pytest.mark.parametrize("error", [30.0, 179.0, -179.0, 180.0])
def test_aoa_bias_is_learned_for_any_array_orientation(error):
    antennas = [Antenna("k", *HOME, bearing_error=error)]
    world = World(antennas, seed=5)
    world.add_genuine(10)
    monitor = make_monitor(world, integrity_config(antennas, kind="aoa"))
    run_world(world, monitor, 60)
    assert abs(geodesy.angle_diff_deg(monitor._bearing_bias["k"], error)) < 1
    for icao in world.planes:
        assert integrity_of(world, icao)["checks"]["aoa"]["status"] == "pass"


def test_aoa_bias_learned_when_aircraft_arrive_one_by_one():
    """An unknown array orientation must not fail (and so distrust) every
    aircraft before enough have arrived to learn it."""
    antennas = [Antenna("k", *HOME, bearing_error=90.0)]
    world = World(antennas, seed=5)
    monitor = make_monitor(world, integrity_config(antennas, kind="aoa"))
    for i in range(12):
        lat, lon = geodesy.destination(
            HOME[0],
            HOME[1],
            world.rng.uniform(0, 360),
            world.rng.uniform(30, 180) * 1000,
        )
        world.add(
            Aircraft(f"a1{i:04x}", lat, lon, 9000, 600, world.rng.uniform(0, 360))
        )
        run_world(world, monitor, 4)
    run_world(world, monitor, 60)
    assert abs(geodesy.angle_diff_deg(monitor._bearing_bias["k"], 90.0)) < 1
    for plane in world.planes.values():
        statuses = {v.status for v in plane.track.integrity_log}
        assert statuses <= {"unknown", STATUS_OK}
        assert plane.info.integrity["status"] == STATUS_OK
        assert plane.info.integrity["checks"]["aoa"]["status"] == "pass"


# --- linear-array AoA -----------------------------------------------------------------


def _linear_array_world(monkeypatch, axis: float, error: float):
    """A two-element array whose baseline points along *axis* (true bearing):
    it measures only the angle to that axis (0-180) and reports it on one
    side, so a bearing and its reflection about the axis read the same. The
    sidecar's reports are *error* degrees high (its mounting assumption)."""
    transmit = World._transmit

    def linear(self, ac, t):
        frames = transmit(self, ac, t)
        out = []
        for f in frames:
            true = (f.bearing - error) % 360.0  # World added error and noise
            reported = (axis + abs(geodesy.angle_diff_deg(true, axis)) + error) % 360.0
            out.append(type(f)(f.hex, f.timestamp, f.receiver, f.rssi, f.clock, reported))
        return out

    monkeypatch.setattr(World, "_transmit", linear)
    antennas = [Antenna("k", *HOME, bearing_error=error)]
    world = World(antennas, seed=5)
    world.add_genuine(12)
    config = integrity_config(antennas, kind="aoa")
    config.receivers["k"] = ReceiverConfig(
        type="aoa",
        port=1234,
        position=ReceiverPositionConfig(
            latitude=HOME[0], longitude=HOME[1], altitude=100.0
        ),
        options={"array_axis": axis},
    )
    return world, make_monitor(world, config)


@pytest.mark.parametrize("error", [0.0, 25.0, -40.0])
def test_linear_array_bias_is_learned_and_checked_only_once_known(monkeypatch, error):
    world, monitor = _linear_array_world(monkeypatch, axis=90.0, error=error)
    seen_before_learned = []
    for _ in range(140):
        monitor.observe_frames(world.step(0.5))
        monitor.evaluate(world.planes, world.t, force=True)
        if "k" not in monitor._bearing_bias:
            seen_before_learned += [
                icao
                for icao in world.planes
                if "aoa" in integrity_of(world, icao)["checks"]
            ]
    assert seen_before_learned == []  # silent until the orientation is known
    assert abs(geodesy.angle_diff_deg(monitor._bearing_bias["k"], error)) < 2
    for icao in world.planes:
        assert integrity_of(world, icao)["checks"]["aoa"]["status"] == "pass", icao
        statuses = {
            v.status for v in world.planes[icao].track.integrity_log
        }
        assert statuses <= {"unknown", STATUS_OK}, (icao, statuses)
