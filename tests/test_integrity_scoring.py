"""How integrity verdicts are published: trust bootstrap, hysteresis,
evaluation scheduling and verdicts carried across flight splits."""

from __future__ import annotations

from integrity_world import (
    HOME,
    Aircraft,
    Antenna,
    World,
    add_spoofer,
    integrity_config,
    integrity_of,
    make_monitor,
    run_world,
)
from pyaerial.classify import CAT_VELOCITY
from pyaerial.integrity import STATUS_OK, STATUS_UNKNOWN, geodesy
from pyaerial.integrity import monitor as monitor_module
from pyaerial.integrity.checks import FAIL, PASS, WARN, CheckResult

# --- Published status and the integrity log ----------------------------------


def test_integrity_log_records_changes_not_refreshes():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(1)
    monitor = make_monitor(world, integrity_config(antennas))
    plane = world.planes["A10000"]
    for i in range(240):
        status = FAIL if 100 <= i < 150 else PASS
        found = {
            "kinematics": CheckResult(status, f"moved {i}", float(i)),
            "replay": CheckResult(PASS, "x"),
            "radio_horizon": CheckResult(PASS, "x"),
        }
        monitor._publish("A10000", plane, found, None, world.t + i)
    log = plane.track.integrity_log
    # unknown until 30 s of evidence; recovery waits _RECOVER_SECONDS.
    assert [(v.time - world.t, v.status) for v in log] == [
        (0, "unknown"),
        (30, "ok"),
        (100, "suspect"),
        # Check recovered while the status was held: details logged, not status.
        (150, "suspect"),
        (179, "ok"),
    ]
    # The live document is still refreshed.
    assert (
        plane.info.integrity["checks"]["kinematics"]["detail"] == "moved 239"
    )


def test_status_agrees_with_published_score():
    # 1 - 0.41 / 2 = 0.795: published as 0.8, so it must read as ok.
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(1)
    monitor = make_monitor(world, integrity_config(antennas, weights={"kinematics": 0.41}))
    plane = world.planes["A10000"]
    found = {
        "kinematics": CheckResult(WARN, "x"),
        "replay": CheckResult(PASS, "x"),
        "radio_horizon": CheckResult(PASS, "x"),
    }
    monitor._publish("A10000", plane, found, None, world.t + 60)
    doc = plane.info.integrity
    assert (doc["score"], doc["status"]) == (0.8, STATUS_OK)


# --- trust bootstrap ----------------------------------------------------------
# New aircraft start unknown and untrusted; worsening is immediate, recovery
# waits.


def _tdoa_world() -> tuple[World, list[Antenna]]:
    antennas = [
        Antenna("a", *HOME, clock_offset=12.3, drift_ppm=3),
        Antenna("b", *geodesy.destination(*HOME, 80, 40_000), clock_offset=-401.7),
        Antenna("c", *geodesy.destination(*HOME, 200, 45_000), clock_offset=77.0),
    ]
    world = World(antennas, seed=11)
    world.add_genuine(10, min_km=20, max_km=120)
    return world, antennas


def test_new_aircraft_is_unknown_and_untrusted_until_enough_evidence():
    world, antennas = _tdoa_world()
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 20)
    for icao in world.planes:
        doc = integrity_of(world, icao)
        assert doc["score"] is None and len(doc["checks"]) >= 3
        assert doc["status"] == STATUS_UNKNOWN
        assert not monitor._trusted(icao)
    # Nothing calibrated from aircraft that have not earned trust.
    assert monitor._rssi_model == {}
    assert all(not pair.points for pair in monitor._sync.pairs.values())

    run_world(world, monitor, 20)
    assert {integrity_of(world, icao)["status"] for icao in world.planes} == {STATUS_OK}
    assert all(pair.fitted for pair in monitor._sync.pairs.values())


def test_failures_publish_without_waiting_for_trust():
    antennas = [Antenna("a", *HOME, alt=10)]
    world = World(antennas)
    lat, lon = geodesy.destination(HOME[0], HOME[1], 90, 300_000)
    world.add(Aircraft("c00001", lat, lon, alt=300, speed_kph=300, track=90))
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 3)
    assert integrity_of(world, "c00001")["status"] == "spoofed"


# --- hysteresis ---------------------------------------------------------------


def _three(status: str) -> dict[str, CheckResult]:
    return {
        "kinematics": CheckResult(status, "x"),
        "replay": CheckResult(PASS, "x"),
        "radio_horizon": CheckResult(PASS, "x"),
    }


def _drive(statuses: list[str]) -> tuple[list[tuple[float, str]], list[str]]:
    """Publish one result per second; return the log and published statuses."""
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(1)
    monitor = make_monitor(world, integrity_config(antennas))
    plane = world.planes["A10000"]
    published = []
    for i, status in enumerate(statuses):
        monitor._publish("A10000", plane, _three(status), None, world.t + i)
        published.append(monitor._statuses["A10000"])
    log = [
        (v.time - world.t, v.status) for v in plane.track.integrity_log
    ]
    return log, published


def test_score_near_threshold_does_not_flap():
    # kinematics (weight 0.4) alternating warn (0.8, ok) / fail (0.6,
    # suspect) at the 5 s check interval for five minutes.
    statuses = [PASS] * 60 + [WARN if (i // 5) % 2 else FAIL for i in range(300)]
    log, published = _drive(statuses)
    changes = [
        s for (_t, s), (_t2, prev) in zip(log[1:], log, strict=False) if s != prev
    ]
    assert changes == ["ok", "suspect"]
    assert set(published[60:]) == {"suspect"}
    # Score/check-only updates under the held status are rate limited.
    assert len(log) <= 3 + 300 // 30


def test_worsening_is_immediate_and_recovery_waits():
    log, published = _drive([PASS] * 60 + [FAIL] + [PASS] * 40)
    assert published[60] == "suspect"
    assert published[60 + 29] == "suspect"
    assert published[60 + 30] == STATUS_OK
    assert [s for _t, s in log] == [STATUS_UNKNOWN, STATUS_OK, "suspect", STATUS_OK]


def test_published_doc_matches_held_status():
    _log, published = _drive([PASS] * 60 + [FAIL] + [PASS] * 5)
    assert published[-1] == "suspect"


def test_genuine_traffic_has_no_status_flaps_over_five_minutes():
    antennas = [
        Antenna("a", *HOME),
        Antenna("b", *geodesy.destination(*HOME, 80, 40_000)),
    ]
    world = World(antennas, seed=1)
    world.add_genuine(20)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 300)
    for plane in world.planes.values():
        statuses = [v.status for v in plane.track.integrity_log]
        assert statuses[-1] == STATUS_OK
        assert [
            s for s, prev in zip(statuses[1:], statuses, strict=False) if s != prev
        ] == [STATUS_OK]
        assert plane.info.integrity["status"] == STATUS_OK


# --- Evaluation spread over ticks --------------------------------------------


def _counting(monkeypatch, name: str) -> list[list[str]]:
    """Record, per evaluate() call, the aircraft passed to monitor.<name>."""
    calls: list[list[str]] = []
    real = getattr(monitor_module.IntegrityMonitor, name)

    def wrapped(self, icao, *args, **kwargs):
        calls[-1].append(icao)
        return real(self, icao, *args, **kwargs)

    monkeypatch.setattr(monitor_module.IntegrityMonitor, name, wrapped)
    return calls


def test_evaluation_is_spread_evenly_over_ticks(monkeypatch):
    antennas = [Antenna("a", *HOME)]
    world = World(antennas, seed=4)
    world.add_genuine(200)
    monitor = make_monitor(world, integrity_config(antennas))
    published = _counting(monkeypatch, "_publish")
    checked = _counting(monkeypatch, "_check_plane")
    for _ in range(80):  # 40 s at 2 Hz, the engine's default, no force
        published.append([])
        checked.append([])
        monitor.observe_frames(world.step(0.5))
        monitor.evaluate(world.planes, world.t)

    # New aircraft: all processed (and checked) on the first call.
    assert len(published[0]) == len(checked[0]) == 200
    steady_pub = [len(c) for c in published[20:]]
    steady_chk = [len(c) for c in checked[20:]]
    # Each tick does about half the fleet, each aircraft once per second...
    assert all(70 <= n <= 130 for n in steady_pub), steady_pub
    for icao in world.planes:
        ticks = [i for i, c in enumerate(published) if icao in c and i >= 20]
        assert {b - a for a, b in zip(ticks, ticks[1:], strict=False)} <= {1, 2, 3}
        assert len(ticks) >= 28  # 60 ticks = 30 s
    # ...and full checks about a tenth of it (every 5 s), never all at once.
    assert max(steady_chk) <= 40 and min(steady_chk) >= 5, steady_chk


def test_failing_aircraft_are_rechecked_every_second(monkeypatch):
    antennas = [Antenna("a", *HOME)]
    world = World(antennas, seed=4)
    world.add_genuine(15)
    add_spoofer(world, "b00001", claim_km=150)
    monitor = make_monitor(world, integrity_config(antennas))
    checked = _counting(monkeypatch, "_check_plane")
    for _ in range(120):
        checked.append([])
        monitor.observe_frames(world.step(0.5))
        monitor.evaluate(world.planes, world.t)
    assert integrity_of(world, "b00001")["status"] != "ok"
    spoof_checks = sum("B00001" in c for c in checked[80:])
    genuine_checks = sum("A10000" in c for c in checked[80:])
    assert spoof_checks >= 18  # once per second over 20 s
    assert 3 <= genuine_checks <= 5  # once per 5 s


# --- Flight splits keep track age, checks and verdict ------------------------


# --- flight splits ------------------------------------------------------------------


def _split(world: World) -> None:
    """What the engine's flight split does to every plane: first_packet moves
    to now, track_started stays."""
    for plane in world.planes.values():
        track = plane.track
        if track.track_started is None:
            track.track_started = track.first_packet
        track.first_packet = world.t


def _statuses_since(world: World, icao: str, t0: float) -> list[str]:
    log = world.planes[icao].track.integrity_log
    return [v.status for v in log if v.time >= t0]


def test_split_flight_keeps_track_age_checks_and_verdict():
    """A spoofer caught by message_mix (no velocity squitters) stays spoofed
    across a flight split; it fell back to unknown for a minute."""
    antennas = [Antenna("a", *HOME)]
    world = World(antennas, seed=2)
    world.add_genuine(6)
    config = integrity_config(antennas, weights={"message_mix": 0.6})
    monitor = make_monitor(world, config)

    def no_velocity(w: World):
        w.planes["A10000"].track.packet_types[CAT_VELOCITY] = 0
        return []

    run_world(world, monitor, 90, extra=no_velocity)
    assert integrity_of(world, "a10000")["status"] == "spoofed"
    genuine = [icao for icao in world.planes if icao != "A10000"]
    assert {integrity_of(world, icao)["status"] for icao in genuine} == {STATUS_OK}
    split_at = world.t
    _split(world)
    run_world(world, monitor, 130, extra=no_velocity)
    assert set(_statuses_since(world, "A10000", split_at)) <= {"spoofed"}
    doc = integrity_of(world, "a10000")
    assert doc["checks"]["message_mix"]["status"] == "fail"
    for icao in genuine:
        assert set(_statuses_since(world, icao, split_at)) <= {STATUS_OK}, icao
        assert "message_mix" in integrity_of(world, icao)["checks"]


def test_track_age_falls_back_to_first_packet():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas, seed=2)
    world.add_genuine(3)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 40)
    # No track_started at all (older engine): first_packet is the track age.
    assert all(p.track.track_started is None for p in world.planes.values())
    assert {integrity_of(world, icao)["status"] for icao in world.planes} == {STATUS_OK}
