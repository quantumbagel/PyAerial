from __future__ import annotations

import random

import pytest

from helpers import make_config
from integrity_world import (
    HOME,
    Aircraft,
    Antenna,
    World,
    df4,
    es_position,
)
from pyaerial.calc.evaluate import integrity_score, make_resolver
from pyaerial.config.schema import (
    IntegrityConfig,
    ReceiverConfig,
    ReceiverPositionConfig,
)
from pyaerial.constants import STORE_INFO, STORE_INTEGRITY_LOG, STORE_INTERNAL
from pyaerial.integrity import STATUS_OK, STATUS_SPOOFED, IntegrityMonitor, checks, geodesy
from pyaerial.integrity.identity import icao_to_n_number
from pyaerial.integrity.tdoa import multilaterate
from pyaerial.receivers.frames import RawFrame
from pyaerial.receivers.positions import ReceiverPositions
from pyaerial.tracker import STORE_GATE_REJECTS
from shapely import Polygon


def _config(antennas: list[Antenna], *, kind: str = "dump1090", **integrity):
    receivers = {
        a.name: ReceiverConfig(
            type=kind,
            port=1234 if kind == "aoa" else None,
            position=ReceiverPositionConfig(latitude=a.lat, longitude=a.lon, altitude=a.alt),
        )
        for a in antennas
    }
    config = make_config(receivers=receivers)
    if integrity:
        config.integrity = IntegrityConfig(**integrity)
    return config


def _monitor(world: World, config) -> IntegrityMonitor:
    positions = ReceiverPositions(config, fetch=False)
    # Started long ago so the appearance check is not in its startup grace.
    return IntegrityMonitor(config, positions, started_at=world.t - 1000)


def _run(world: World, monitor: IntegrityMonitor, seconds: float, extra=None) -> None:
    steps = int(seconds / 0.5)
    for _ in range(steps):
        frames = world.step(0.5)
        if extra is not None:
            frames.extend(extra(world))
        monitor.observe_frames(frames)
        monitor.evaluate(world.planes, world.t, force=True)


def _integrity(world: World, icao: str) -> dict:
    return world.planes[icao.upper()][STORE_INFO]["integrity"]


def _spoof(world: World, icao: str, emitter_km: float = 5.0, claim_km: float = 150.0) -> Aircraft:
    """A target claimed far away but radiated from a transmitter near home."""
    lat, lon = geodesy.destination(HOME[0], HOME[1], 45, claim_km * 1000)
    elat, elon = geodesy.destination(HOME[0], HOME[1], 200, emitter_km * 1000)
    return world.add(
        Aircraft(
            icao=icao,
            lat=lat,
            lon=lon,
            alt=9000,
            speed_kph=700,
            track=270,
            emitter=(elat, elon, 50.0),
        )
    )


# --- geometry / helpers ------------------------------------------------------


def test_geodesy_basics():
    km = geodesy.ground_distance_km(35.0, -78.0, 36.0, -78.0)
    assert km == pytest.approx(111.2, abs=0.3)
    assert geodesy.initial_bearing_deg(35.0, -78.0, 36.0, -78.0) == pytest.approx(0.0, abs=0.01)
    assert geodesy.initial_bearing_deg(35.0, -78.0, 35.0, -77.0) == pytest.approx(89.7, abs=0.3)
    # 10 m antenna, 10 km aircraft: ~13 + 412 km.
    assert geodesy.radio_horizon_km(10, 10_000) == pytest.approx(425, abs=2)
    lat, lon, alt = geodesy.geodetic(*geodesy.ecef(35.5, -78.5, 1234.0))
    assert (lat, lon, alt) == pytest.approx((35.5, -78.5, 1234.0), abs=1e-6)
    assert geodesy.angle_diff_deg(10, 350) == pytest.approx(20)
    assert geodesy.angle_diff_deg(350, 10) == pytest.approx(-20)


def test_n_number_mapping():
    assert icao_to_n_number("A00001") == "N1"
    assert icao_to_n_number("A00002") == "N1A"
    assert icao_to_n_number("ADF7C7") == "N99999"
    assert icao_to_n_number("400000") is None


def test_identity_flags_mismatched_n_number_callsign():
    assert checks.identity("A00001", "N1", None).status == checks.PASS
    assert checks.identity("A00001", "N123AB", None).status == checks.FAIL
    assert checks.identity("400000", "N123AB", None).status == checks.FAIL
    assert checks.identity("400000", "BAW123", None) is None


def test_timing_check_separates_random_from_periodic():
    rng = random.Random(1)
    clock = 0
    real = []
    for i in range(40):
        clock += int(rng.uniform(0.4, 0.6) * 12e6)
        real.append((clock, i % 2))
    assert checks.timing(real).status == checks.PASS

    periodic = [(int(i * 0.5 * 12e6), i % 2) for i in range(40)]
    assert checks.timing(periodic).status == checks.FAIL

    clock = 0
    no_alternation = []
    for _ in range(40):
        clock += int(rng.uniform(0.4, 0.6) * 12e6)
        no_alternation.append((clock, 0))
    assert checks.timing(no_alternation).status == checks.FAIL


def test_kinematics_check():
    ok = checks.kinematics(
        elapsed_s=15,
        distance_m=3000,
        displacement_bearing=90,
        reported_speed_kph=720,
        reported_tracks=[90, 91],
        climb_mps=5,
        reported_vrate_mps=5,
    )
    assert ok.status == checks.PASS
    bad = checks.kinematics(
        elapsed_s=15,
        distance_m=3000,
        displacement_bearing=90,
        reported_speed_kph=300,
        reported_tracks=[180],
        climb_mps=0,
        reported_vrate_mps=20,
    )
    assert bad.status == checks.FAIL


def test_multilaterate_recovers_emitter():
    rx = [
        geodesy.ecef(35.70, -78.70, 100),
        geodesy.ecef(35.90, -78.50, 100),
        geodesy.ecef(35.50, -78.40, 100),
        geodesy.ecef(35.80, -78.95, 100),
    ]
    truth = geodesy.ecef(35.75, -78.62, 9000)
    ranges = [geodesy.slant_range_m(r, truth) for r in rx]
    others = [(rx[i], ranges[i] - ranges[0]) for i in range(1, 4)]
    start = geodesy.ecef(35.6, -78.8, 8000)
    solved, rms = multilaterate(rx[0], others, start, fixed_altitude=9000)
    assert rms < 1.0
    assert geodesy.slant_range_m(solved, truth) < 50


# --- monitor end to end ------------------------------------------------------


def test_genuine_traffic_scores_ok():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 40)
    statuses = {icao: plane[STORE_INFO]["integrity"]["status"] for icao, plane in world.planes.items()}
    assert set(statuses.values()) == {STATUS_OK}


def test_verdict_changes_are_logged_on_the_plane():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(5)
    _spoof(world, "b00001", claim_km=150)
    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 40)
    plane = world.planes["B00001"]
    verdicts = plane[STORE_INTERNAL][STORE_INTEGRITY_LOG]
    assert [v.time for v in verdicts] == sorted(v.time for v in verdicts)
    latest = plane[STORE_INFO]["integrity"]
    assert verdicts[-1].status == latest["status"]
    assert verdicts[-1].checks == latest["checks"]
    assert set(verdicts[-1].flagged) == {
        name for name, check in latest["checks"].items() if check["status"] != "pass"
    }


def test_spoofer_fails_signal_and_horizon_checks():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    _spoof(world, "b00001", claim_km=150)
    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 40)
    doc = _integrity(world, "b00001")
    assert doc["checks"]["rssi_range"]["status"] == "fail"
    assert doc["score"] < 1.0


def test_spoofed_fleet_is_flagged_as_one_transmitter():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas, seed=3)
    world.add_genuine(15)
    for i in range(3):
        _spoof(world, f"b0000{i}", claim_km=120 + 20 * i)
    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 40)
    for i in range(3):
        doc = _integrity(world, f"b0000{i}")
        assert doc["checks"]["emitter_cluster"]["status"] == "fail"
        assert doc["status"] != STATUS_OK


def test_beyond_radio_horizon_fails():
    antennas = [Antenna("a", *HOME, alt=10)]
    world = World(antennas)
    lat, lon = geodesy.destination(HOME[0], HOME[1], 90, 300_000)
    low = Aircraft("c00001", lat, lon, alt=300, speed_kph=300, track=90, emitter=(HOME[0] + 0.05, HOME[1], 10))
    world.add(low)
    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 5)
    assert _integrity(world, "c00001")["checks"]["radio_horizon"]["status"] == "fail"


def test_aoa_mismatch_and_bearing_cluster():
    antennas = [Antenna("k", *HOME)]
    world = World(antennas, seed=5)
    world.add_genuine(10)
    _spoof(world, "b10001", claim_km=100)
    _spoof(world, "b10002", claim_km=140)
    config = _config(antennas, kind="aoa")
    monitor = _monitor(world, config)
    _run(world, monitor, 30)
    for icao in ("b10001", "b10002"):
        doc = _integrity(world, icao)
        assert doc["checks"]["aoa"]["status"] == "fail"
        assert doc["checks"]["emitter_cluster"]["status"] == "fail"
    genuine = _integrity(world, "a10000")
    assert genuine["checks"]["aoa"]["status"] == "pass"


def test_tdoa_flags_spoofer_and_locates_emitter():
    antennas = [
        Antenna("a", *HOME, clock_offset=12.3, drift_ppm=3),
        Antenna("b", *geodesy.destination(*HOME, 80, 40_000), clock_offset=-401.7, drift_ppm=-8),
        Antenna("c", *geodesy.destination(*HOME, 200, 45_000), clock_offset=77.0, drift_ppm=11),
        Antenna("d", *geodesy.destination(*HOME, 330, 35_000), clock_offset=5.5),
    ]
    world = World(antennas, seed=11)
    world.add_genuine(12, min_km=20, max_km=120)
    spoof = _spoof(world, "b20001", emitter_km=8, claim_km=90)
    monitor = _monitor(world, _config(antennas, disabled_checks=["rssi_range", "rssi_trend", "emitter_cluster"]))
    _run(world, monitor, 40)
    doc = _integrity(world, "b20001")
    assert doc["checks"]["tdoa"]["status"] == "fail"
    emitter = doc["emitter"]
    elat, elon, _ = spoof.emitter
    assert geodesy.ground_distance_km(emitter["latitude"], emitter["longitude"], elat, elon) < 3
    assert _integrity(world, "a10000")["checks"]["tdoa"]["status"] == "pass"


def test_mode_s_altitude_mismatch_and_corroboration():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(8)
    for ac in world.aircraft:
        ac.vrate = 0.0

    def replies(w: World) -> list[RawFrame]:
        out = []
        for ac in w.aircraft:
            feet = int(ac.alt / 0.3048 / 25) * 25
            if ac.icao == "a10000":
                feet += 3000  # a clone: the real aircraft is elsewhere
            out.append(RawFrame(df4(ac.icao, feet), w.t, "a"))
        return out

    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 70, extra=replies)
    assert _integrity(world, "a10000")["checks"]["mode_s_altitude"]["status"] == "fail"
    assert _integrity(world, "a10001")["checks"]["mode_s_altitude"]["status"] == "pass"
    assert _integrity(world, "a10001")["checks"]["mode_s_corroboration"]["status"] == "pass"


def test_replayed_frames_and_duplicate_icao():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(3)
    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 5)
    frame = es_position("a10000", 0, salt=42)
    monitor.observe_frames([RawFrame(frame, world.t, "a"), RawFrame(frame, world.t + 1, "a")])
    monitor.observe_frames([RawFrame(frame, world.t + 2, "a"), RawFrame(frame, world.t + 3, "a")])
    world.planes["A10000"][STORE_INTERNAL][STORE_GATE_REJECTS] = 4
    monitor.evaluate(world.planes, world.t + 3, force=True)
    doc = _integrity(world, "a10000")
    assert doc["checks"]["replay"]["status"] == "fail"
    assert doc["checks"]["duplicate_icao"]["status"] == "fail"


def test_score_feeds_zone_rules_and_weights_are_configurable():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    _spoof(world, "b30001")
    config = _config(antennas, weights={"rssi_range": 1.0})
    monitor = _monitor(world, config)
    _run(world, monitor, 40)
    plane = world.planes["B30001"]
    assert _integrity(world, "b30001")["status"] == STATUS_SPOOFED
    resolve = make_resolver(plane, 0, Polygon([(0, 0), (0, 1), (1, 1)]), (0, 0))
    assert resolve("integrity") == integrity_score(plane) == 0.0


def test_forget_drops_state():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(2)
    monitor = _monitor(world, _config(antennas))
    _run(world, monitor, 3)
    assert monitor.score("a10000") is not None
    monitor.forget("a10000")
    assert monitor.score("a10000") is None


def test_unknown_integrity_check_name_rejected():
    with pytest.raises(ValueError, match="unknown integrity check"):
        IntegrityConfig(weights={"bogus": 0.5})


def test_tis_b_rebroadcast_skips_signal_geometry_checks():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    spoof = _spoof(world, "b40001")
    monitor = _monitor(world, _config(antennas))

    def as_tis_b(frames: list[RawFrame]) -> list[RawFrame]:
        # DF18, CF=2 (fine TIS-B): first byte 0x92.
        return [
            RawFrame("92" + f.hex[2:], f.timestamp, f.receiver, f.rssi, f.clock)
            if f.hex[2:8] == spoof.icao
            else f
            for f in frames
        ]

    for _ in range(80):
        monitor.observe_frames(as_tis_b(world.step(0.5)))
        monitor.evaluate(world.planes, world.t, force=True)
    doc = _integrity(world, "b40001")
    assert doc["rebroadcast"] is True
    assert "rssi_range" not in doc["checks"]
    assert doc["status"] == STATUS_OK


def test_mlat_frames_are_not_evidence():
    from pyaerial.receivers.frames import BEAST_MLAT_CLOCK

    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    monitor = _monitor(world, _config(antennas))
    frame = es_position("a10000", 0)
    monitor.observe_frames([RawFrame(frame, 1.0, "a", -5.0, BEAST_MLAT_CLOCK)])
    assert monitor._evidence == {}
