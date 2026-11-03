from __future__ import annotations

import random
import statistics
from dataclasses import replace

import pytest
from shapely import Polygon

from integrity_world import (
    HOME,
    Aircraft,
    Antenna,
    World,
    add_spoofer,
    df4,
    es_position,
    integrity_config,
    integrity_of,
    make_monitor,
    new_plane,
    run_world,
    with_crc,
)
from pyaerial.calc.evaluate import integrity_score, make_resolver
from pyaerial.classify import CAT_AIRBORNE_GNSS
from pyaerial.config.schema import IntegrityConfig
from pyaerial.constants import (
    STORE_GATE_REJECTS,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_INFO,
    STORE_INTEGRITY_LOG,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_PACKET_TYPE,
    STORE_RECV_DATA,
)
from pyaerial.integrity import (
    STATUS_OK,
    STATUS_SPOOFED,
    checks,
    geodesy,
)
from pyaerial.integrity.checks import FAIL, PASS
from pyaerial.integrity.evidence import latest_fix
from pyaerial.integrity.identity import icao_to_n_number
from pyaerial.integrity.stats import fast_median, pstdev
from pyaerial.integrity.tdoa import multilaterate
from pyaerial.models import Datum
from pyaerial.receivers.frames import RawFrame

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
        real.append((clock / 12e6, clock, i % 2))
    assert checks.timing(real).status == checks.PASS

    periodic = [(i * 0.5, int(i * 0.5 * 12e6), i % 2) for i in range(40)]
    assert checks.timing(periodic).status == checks.FAIL

    clock = 0
    no_alternation = []
    for _ in range(40):
        clock += int(rng.uniform(0.4, 0.6) * 12e6)
        no_alternation.append((clock / 12e6, clock, 0))
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
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 40)
    statuses = {icao: plane[STORE_INFO]["integrity"]["status"] for icao, plane in world.planes.items()}
    assert set(statuses.values()) == {STATUS_OK}


def test_verdict_changes_are_logged_on_the_plane():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(5)
    add_spoofer(world, "b00001", claim_km=150)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 40)
    plane = world.planes["B00001"]
    verdicts = plane[STORE_INTERNAL][STORE_INTEGRITY_LOG]
    assert [v.time for v in verdicts] == sorted(v.time for v in verdicts)
    latest = plane[STORE_INFO]["integrity"]
    assert verdicts[-1].status == latest["status"]
    assert verdicts[-1].score == latest["score"]
    # Details refresh live; only a changed verdict is logged again.
    assert {n: c["status"] for n, c in verdicts[-1].checks.items()} == {
        n: c["status"] for n, c in latest["checks"].items()
    }
    assert set(verdicts[-1].flagged) == {
        name for name, check in latest["checks"].items() if check["status"] != "pass"
    }


def test_spoofer_fails_signal_and_horizon_checks():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    add_spoofer(world, "b00001", claim_km=150)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 40)
    doc = integrity_of(world, "b00001")
    assert doc["checks"]["rssi_range"]["status"] == "fail"
    assert doc["score"] < 1.0


def test_spoofed_fleet_is_flagged_as_one_transmitter():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas, seed=3)
    world.add_genuine(15)
    for i in range(3):
        add_spoofer(world, f"b0000{i}", claim_km=120 + 20 * i)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 40)
    for i in range(3):
        doc = integrity_of(world, f"b0000{i}")
        assert doc["checks"]["emitter_cluster"]["status"] == "fail"
        assert doc["status"] != STATUS_OK


def test_beyond_radio_horizon_fails():
    antennas = [Antenna("a", *HOME, alt=10)]
    world = World(antennas)
    lat, lon = geodesy.destination(HOME[0], HOME[1], 90, 300_000)
    low = Aircraft("c00001", lat, lon, alt=300, speed_kph=300, track=90, emitter=(HOME[0] + 0.05, HOME[1], 10))
    world.add(low)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 5)
    assert integrity_of(world, "c00001")["checks"]["radio_horizon"]["status"] == "fail"


def test_aoa_mismatch_and_bearing_cluster():
    antennas = [Antenna("k", *HOME)]
    world = World(antennas, seed=5)
    world.add_genuine(10)
    add_spoofer(world, "b10001", claim_km=100)
    add_spoofer(world, "b10002", claim_km=140)
    config = integrity_config(antennas, kind="aoa")
    monitor = make_monitor(world, config)
    # Bias is learned once aircraft are trusted (30 s of evidence).
    run_world(world, monitor, 60)
    for icao in ("b10001", "b10002"):
        doc = integrity_of(world, icao)
        assert doc["checks"]["aoa"]["status"] == "fail"
        assert doc["checks"]["emitter_cluster"]["status"] == "fail"
    genuine = integrity_of(world, "a10000")
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
    spoof = add_spoofer(world, "b20001", emitter_km=8, claim_km=90)
    monitor = make_monitor(world, integrity_config(antennas, disabled_checks=["rssi_range", "rssi_trend", "emitter_cluster"]))
    run_world(world, monitor, 40)
    doc = integrity_of(world, "b20001")
    assert doc["checks"]["tdoa"]["status"] == "fail"
    emitter = doc["emitter"]
    elat, elon, _ = spoof.emitter
    assert geodesy.ground_distance_km(emitter["latitude"], emitter["longitude"], elat, elon) < 3
    assert integrity_of(world, "a10000")["checks"]["tdoa"]["status"] == "pass"


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

    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 70, extra=replies)
    assert integrity_of(world, "a10000")["checks"]["mode_s_altitude"]["status"] == "fail"
    assert integrity_of(world, "a10001")["checks"]["mode_s_altitude"]["status"] == "pass"
    assert integrity_of(world, "a10001")["checks"]["mode_s_corroboration"]["status"] == "pass"


def test_replayed_frames_and_duplicate_icao():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(3)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 5)
    frame = es_position("a10000", 0, salt=42)
    monitor.observe_frames([RawFrame(frame, world.t, "a"), RawFrame(frame, world.t + 1, "a")])
    monitor.observe_frames([RawFrame(frame, world.t + 2, "a"), RawFrame(frame, world.t + 3, "a")])
    world.planes["A10000"][STORE_INTERNAL][STORE_GATE_REJECTS] = 4
    monitor.evaluate(world.planes, world.t + 3, force=True)
    doc = integrity_of(world, "a10000")
    assert doc["checks"]["replay"]["status"] == "fail"
    assert doc["checks"]["duplicate_icao"]["status"] == "fail"


def test_score_feeds_zone_rules_and_weights_are_configurable():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    add_spoofer(world, "b30001")
    config = integrity_config(antennas, weights={"rssi_range": 1.0})
    monitor = make_monitor(world, config)
    run_world(world, monitor, 40)
    plane = world.planes["B30001"]
    assert integrity_of(world, "b30001")["status"] == STATUS_SPOOFED
    resolve = make_resolver(plane, 0, Polygon([(0, 0), (0, 1), (1, 1)]), (0, 0))
    assert resolve("integrity") == integrity_score(plane) == 0.0


def test_forget_drops_state():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(2)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 3)
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
    spoof = add_spoofer(world, "b40001")
    monitor = make_monitor(world, integrity_config(antennas))

    def as_tis_b(frames: list[RawFrame]) -> list[RawFrame]:
        # DF18, CF=2 (fine TIS-B): first byte 0x92.
        return [
            RawFrame(with_crc("92" + f.hex[2:]), f.timestamp, f.receiver, f.rssi, f.clock)
            if f.hex[2:8] == spoof.icao
            else f
            for f in frames
        ]

    for _ in range(80):
        monitor.observe_frames(as_tis_b(world.step(0.5)))
        monitor.evaluate(world.planes, world.t, force=True)
    doc = integrity_of(world, "b40001")
    assert doc["rebroadcast"] is True
    assert "rssi_range" not in doc["checks"]
    assert doc["status"] == STATUS_OK


def test_mlat_frames_are_not_evidence():
    from pyaerial.receivers.frames import BEAST_MLAT_CLOCK

    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    monitor = make_monitor(world, integrity_config(antennas))
    frame = es_position("a10000", 0)
    monitor.observe_frames([RawFrame(frame, 1.0, "a", -5.0, BEAST_MLAT_CLOCK)])
    assert monitor._evidence == {}


# --- Mode S altitude --------------------------------------------------------------


def test_mode_s_altitude_runs_for_aircraft_mostly_sending_gnss_positions():
    """``altitude`` only ever holds barometric height, so an aircraft whose
    positions are mostly GNSS-height (TC 20-22) is still checked against it."""
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(2)
    for ac in world.aircraft:
        ac.vrate = 0.0

    def replies(w: World) -> list[RawFrame]:
        out = []
        for ac in w.aircraft:
            feet = int(ac.alt / 0.3048 / 25) * 25 + (3000 if ac.icao == "a10000" else 0)
            out.append(RawFrame(df4(ac.icao, feet), w.t, "a"))
        return out

    monitor = make_monitor(world, integrity_config(antennas))
    for plane in world.planes.values():
        plane[STORE_INTERNAL][STORE_PACKET_TYPE][CAT_AIRBORNE_GNSS] = 10_000
    run_world(world, monitor, 20, extra=replies)
    assert integrity_of(world, "a10000")["checks"]["mode_s_altitude"]["status"] == "fail"
    assert integrity_of(world, "a10001")["checks"]["mode_s_altitude"]["status"] == "pass"


# --- Signal, kinematics and horizon checks -----------------------------------


def test_kinematics_sees_steady_reported_velocity():
    """The tracker compresses a constant speed/track to one run; the check
    must still compare against it (a spoofer often reports exactly that)."""
    t0 = 1000.0
    plane = new_plane("A10000", t0)
    recv = plane[STORE_RECV_DATA]
    recv[STORE_LAT], recv[STORE_LONG] = [], []
    for i in range(61):
        t = t0 + i * 0.5
        lat, lon = geodesy.destination(HOME[0], HOME[1], 90, 200 * i * 0.5)
        recv[STORE_LAT].append(Datum(lat, t))
        recv[STORE_LONG].append(Datum(lon, t))
    # Moves east at 720 km/h but steadily claims 300 km/h due north.
    recv[STORE_HORIZ_SPEED] = [Datum(300.0, t0), Datum(300.0, t0 + 30.2)]
    recv[STORE_HEADING] = [Datum(0.0, t0), Datum(0.0, t0 + 30.2)]
    antennas = [Antenna("a", *HOME)]
    monitor = make_monitor(World(antennas), integrity_config(antennas))
    result = monitor._kinematics(plane, latest_fix(plane))
    assert result is not None and result.status == FAIL


def test_saturating_spoofer_still_fails_rssi_range(monkeypatch):
    """A transmitter close enough to clip the receiver is the case rssi_range
    exists for; clipped samples are a lower bound on its signal."""
    transmit = World._transmit

    def clipped(self, ac, t):
        frames = transmit(self, ac, t)
        if ac.emitter is None:
            return frames
        return [replace(f, rssi=-0.5) for f in frames]

    monkeypatch.setattr(World, "_transmit", clipped)
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    add_spoofer(world, "b00001", claim_km=150)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 40)
    assert integrity_of(world, "b00001")["checks"]["rssi_range"]["status"] == "fail"
    assert integrity_of(world, "a10000")["checks"]["rssi_range"]["status"] == "pass"


def test_radio_horizon_range_cap_applies_to_every_receiver():
    # x is inside its (high-aircraft) horizon but beyond max_range_km; y has
    # the worse horizon ratio.
    result = checks.radio_horizon([("x", 250.0, 583.0), ("y", 100.0, 150.0)], 200.0)
    assert result.status == FAIL and result.detail.startswith("x ")
    assert checks.radio_horizon([("y", 100.0, 150.0)], 200.0).status == PASS


# --- Fast statistics helpers match the standard library ----------------------


def test_fast_stats_match_the_standard_library():
    rng = random.Random(3)
    for _ in range(2000):
        values = [rng.gauss(0.5, 0.06) for _ in range(rng.randint(1, 80))]
        assert fast_median(iter(values)) == statistics.median(values)
        assert abs(pstdev(values) - statistics.pstdev(values)) <= 4e-16 * max(
            1.0, statistics.pstdev(values)
        )
