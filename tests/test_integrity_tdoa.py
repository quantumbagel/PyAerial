"""Receiver clock synchronisation and TDOA: sync windows, clock
discontinuities, copy pairing, clock-pair fits and multilateration evidence."""

from __future__ import annotations

import random
from collections import Counter

from helpers import make_config
from integrity_world import (
    HOME,
    Antenna,
    World,
    add_spoofer,
    drifting_receivers,
    integrity_config,
    integrity_of,
    make_monitor,
    run_world,
    with_crc,
    with_velocity,
)
from pyaerial.config.schema import ReceiverConfig
from pyaerial.constants import (
    STORE_INTEGRITY_LOG,
    STORE_INTERNAL,
)
from pyaerial.integrity import STATUS_OK, geodesy
from pyaerial.integrity.tdoa import (
    METRES_PER_TICK,
    ClockSync,
    PairModel,
    TdoaObservation,
)
from pyaerial.receivers.frames import BEAST_CLOCK_HZ, BEAST_CLOCK_MOD, RawFrame
from pyaerial.receivers.positions import ReceiverPositions

# --- Receiver clocks ---------------------------------------------------------
# Sync window, discontinuities (restart, replay loop) and timing samples
# bounded by time.


def _non_ok_since(world: World, t0: float) -> dict[str, list[str]]:
    out = {}
    for icao, plane in world.planes.items():
        bad = [
            f"{v.status}@{v.time - t0:.0f}s {v.flagged}"
            for v in plane[STORE_INTERNAL][STORE_INTEGRITY_LOG]
            if v.time >= t0 and v.status != STATUS_OK
        ]
        if bad:
            out[icao] = bad
    return out


# --- discontinuities ------------------------------------------------------------


def test_receiver_restart_does_not_flag_aircraft_or_poison_sync():
    antennas = drifting_receivers()
    world = World(antennas, seed=11)
    world.add_genuine(10, min_km=20, max_km=120)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 60)
    assert all(m.fitted for m in monitor._sync.pairs.values())
    restart = world.t
    antennas[1].clock_offset = -world.t + 3.0  # b restarts: clock near zero
    run_world(world, monitor, 60)
    assert _non_ok_since(world, restart) == {}
    for (a, b), model in monitor._sync.pairs.items():
        assert model.fitted, (a, b)
        assert model.spread_m < 200, (a, b, model.spread_m)
    assert {
        integrity_of(world, icao)["checks"]["tdoa"]["status"] for icao in world.planes
    } == {"pass"}


def test_replay_loop_clock_rewind_does_not_fail_timing():
    """A looping capture replays the same clocks again; the timing check must
    not merge the two passes."""
    antennas = [Antenna("a", *HOME)]
    world = World(antennas, seed=3)
    world.add_genuine(10)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 50)
    rewind = world.t
    antennas[0].clock_offset -= 40.0  # clock jumps back into its own past
    run_world(world, monitor, 60)
    assert _non_ok_since(world, rewind) == {}
    for icao in world.planes:
        assert integrity_of(world, icao)["checks"]["timing"]["status"] == "pass"


def test_clock_jump_resets_only_that_receivers_pairs():
    antennas = drifting_receivers()
    world = World(antennas, seed=11)
    world.add_genuine(10, min_km=20, max_km=120)
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 60)
    antennas[3].clock_offset += 500.0
    frames = world.step(0.5)
    monitor.observe_frames(frames)
    assert all(
        not m.points for (a, b), m in monitor._sync.pairs.items() if "d" in (a, b)
    )
    assert all(
        m.fitted for (a, b), m in monitor._sync.pairs.items() if "d" not in (a, b)
    )


def test_wall_clock_jitter_is_not_a_discontinuity():
    antennas = drifting_receivers()
    world = World(antennas, seed=11)
    world.add_genuine(10, min_km=20, max_km=120)
    monitor = make_monitor(world, integrity_config(antennas))
    for i in range(160):
        frames = world.step(0.5)
        # Engine receive times lag the transmission by up to 300 ms.
        lag = 0.3 * (i % 3) / 2
        monitor.observe_frames(
            [
                RawFrame(
                    f.hex, f.timestamp + lag, f.receiver, f.rssi, f.clock, f.bearing
                )
                for f in frames
            ]
        )
        monitor.evaluate(world.planes, world.t, force=True)
    assert monitor._clock_jumps == {}


# --- timing samples bounded by time -------------------------------------------


def test_timing_samples_are_bounded_by_window():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(2)
    monitor = make_monitor(world, integrity_config(antennas, window_seconds=30))
    run_world(world, monitor, 90)
    rows = monitor._evidence["A10000"].pos_clock["a"]
    assert rows[-1][0] - rows[0][0] <= 31
    assert 40 <= len(rows) <= 70  # 2 positions/s over 30 s
    assert integrity_of(world, "a10000")["checks"]["timing"]["status"] == "pass"


def test_timing_samples_keep_a_hard_cap():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(1)
    monitor = make_monitor(world, integrity_config(antennas, window_seconds=600))
    ac = world.aircraft[0]
    ac.period = 0.01  # a flood: 100 position frames per second
    run_world(world, monitor, 20)
    rows = monitor._evidence["A10000"].pos_clock["a"]
    assert len(rows) <= rows.maxlen <= 400


# --- clock-sync window ------------------------------------------------------------


def test_clock_sync_points_span_the_window_in_busy_sky():
    """With many trusted aircraft a 400-point FIFO covered well under a second,
    so drift was fitted from noise. Points now span the 30 s sync window."""
    antennas = drifting_receivers()
    world = World(antennas, seed=11)
    world.add_genuine(80, min_km=20, max_km=150)  # 160 frames/s per pair
    monitor = make_monitor(world, integrity_config(antennas))
    run_world(world, monitor, 75)
    by_name = {a.name: a for a in antennas}
    for (a, b), model in monitor._sync.pairs.items():
        assert model.fitted
        assert model.points[-1][0] - model.points[0][0] >= 25
        assert len({icao for _t, _y, icao in model.points}) >= 40
        true_ppm = by_name[a].drift_ppm - by_name[b].drift_ppm
        assert abs(model.slope / BEAST_CLOCK_HZ * 1e6 - true_ppm) < 0.01


# --- Clock sync and new spoofers ---------------------------------------------


def test_new_spoofer_does_not_reset_clock_sync():
    """An aircraft without a verdict yet is not trusted for calibration: a
    spoofer's first second of frames once reset the receiver clock models."""
    antennas = [
        Antenna("a", *HOME, clock_offset=12.3, drift_ppm=3),
        Antenna("b", *geodesy.destination(*HOME, 80, 60_000), clock_offset=-401.7),
        Antenna("c", *geodesy.destination(*HOME, 200, 65_000), clock_offset=77.0),
        Antenna("d", *geodesy.destination(*HOME, 330, 55_000), clock_offset=5.5),
    ]
    world = World(antennas, seed=11)
    world.add_genuine(8, min_km=20, max_km=120)
    monitor = make_monitor(world, integrity_config(antennas))

    def tick() -> None:
        monitor.observe_frames(world.step(1.0))
        monitor.evaluate(world.planes, world.t, force=True)

    for _ in range(60):
        tick()
    assert monitor._sync.pairs and all(m.fitted for m in monitor._sync.pairs.values())
    # ~7 frames per tick, as a transponder's position + velocity + ident.
    add_spoofer(world, "b20001", emitter_km=8, claim_km=150).period = 0.15
    tick()
    assert all(m.fitted for m in monitor._sync.pairs.values())
    for _ in range(5):
        tick()
    assert integrity_of(world, "b20001")["checks"]["tdoa"]["status"] == "fail"


# --- Clock pairs: bootstrap spoofers and far-off points ----------------------


def test_spoofer_trusted_during_bootstrap_is_dropped_from_clock_sync():
    """Every aircraft earns trust at the same moment after start-up, spoofer
    included, and its frames enter the clock fits. Once its TDOA fails they
    are removed; left in, they skewed the fits and the multilaterated emitter
    landed kilometres off (seen at 20 Hz ticks with a full message mix)."""
    antennas = drifting_receivers()
    world = World(antennas, seed=1)
    world.add_genuine(10, min_km=25, max_km=120)
    spoof = add_spoofer(world, "b20001", emitter_km=8, claim_km=90)
    monitor = make_monitor(world, integrity_config(antennas))
    worst = 0.0
    for _ in range(int(60 / 0.05)):
        monitor.observe_frames(with_velocity(world.step(0.05)))
        monitor.evaluate(world.planes, world.t)
        cached = monitor._cached.get("B20001")
        if cached and cached[1]:
            emitter = cached[1]
            worst = max(
                worst,
                geodesy.ground_distance_km(
                    emitter["latitude"], emitter["longitude"], *spoof.emitter[:2]
                ),
            )
            for model in monitor._sync.pairs.values():
                assert all(p[2] != "B20001" for p in model.points)
    assert integrity_of(world, "b20001")["checks"]["tdoa"]["status"] == "fail"
    assert 0 < worst < 2


def _fitted_pair() -> PairModel:
    model = PairModel("a", "b")
    for i in range(40):
        model.add(1000.0 + i * 0.5, 1e6, f"ac{i % 4}")
    model.refit(1020.0)
    assert model.fitted
    return model


def test_one_aircraft_far_off_does_not_reset_a_clock_pair():
    model = _fitted_pair()
    far = 1e6 + 100_000 / METRES_PER_TICK  # 100 km off the model
    for i in range(10):  # a spoofer's burst of frames, back to back
        model.add(1020.1 + i * 0.01, far, "spoof")
    assert model.fitted and model.points


def test_far_off_points_from_several_aircraft_reset_a_clock_pair():
    model = _fitted_pair()
    far = 1e6 + 100_000 / METRES_PER_TICK  # a receiver restart moves them all
    for i in range(6):
        model.add(1020.1 + i * 0.01, far, f"ac{i % 3}")
    assert not model.fitted and not model.points


# --- TDOA copy pairing ----------------------------------------------------------


def _steady_velocity(icao: str) -> str:
    """A steady-cruise velocity squitter: every transmission is bit-identical."""
    me = (19 << 51) | (1 << 48) | 0x12345678
    return with_crc(f"8d{icao.lower()}{me:014x}000000")


def _two_receivers():
    config = make_config(
        receivers={
            "a": ReceiverConfig(type="dump1090"),
            "b": ReceiverConfig(type="dump1090"),
        }
    )
    from pyaerial.integrity import IntegrityMonitor

    return IntegrityMonitor(config, ReceiverPositions(config, fetch=False))


def test_repeated_squitters_heard_by_different_receivers_are_not_paired():
    """a decodes only the first transmission, b only the second (0.4 s later):
    two transmissions, not one heard twice."""
    monitor = _two_receivers()
    v = _steady_velocity("abc123")
    t, ca, cb = 1_790_000_000.0, 10_000_000, 55_000_000
    monitor.observe_frames(
        [
            RawFrame(v, t, "a", -20.0, ca),
            RawFrame(v, t + 0.4, "b", -20.0, cb + int(0.4 * BEAST_CLOCK_HZ)),
        ]
    )
    ev = monitor._evidence["ABC123"]
    assert list(ev.tdoa) == []


def test_copies_of_one_transmission_pair_despite_delivery_jitter():
    monitor = _two_receivers()
    v = _steady_velocity("abc123")
    t = 1_790_000_000.0
    monitor.observe_frames(
        [
            RawFrame(v, t, "a", -20.0, 10_000_000),
            RawFrame(v, t + 0.06, "b", -20.0, 55_000_000),  # b's feed lags 60 ms
            # the next transmission, heard by both
            RawFrame(v, t + 0.5, "a", -20.0, 16_000_000),
            RawFrame(v, t + 0.52, "b", -20.0, 61_000_100),
        ]
    )
    obs = list(monitor._evidence["ABC123"].tdoa)
    assert [o.clocks for o in obs] == [
        {"a": 10_000_000, "b": 55_000_000},
        {"a": 16_000_000, "b": 61_000_100},
    ]
    assert [o.time for o in obs] == [t, t + 0.5]


def _steady_receivers() -> list[Antenna]:
    return [
        Antenna("a", *HOME, clock_offset=12.3, drift_ppm=3),
        Antenna("b", *geodesy.destination(*HOME, 80, 40_000), clock_offset=-401.7),
        Antenna("c", *geodesy.destination(*HOME, 200, 45_000), clock_offset=77.0),
        Antenna("d", *geodesy.destination(*HOME, 330, 35_000), clock_offset=5.5),
    ]


def test_lossy_receivers_and_identical_squitters_leave_residuals_physical():
    """Receivers each miss half the frames; velocity squitters repeat
    bit-for-bit. No residual may imply more than the receiver baseline."""
    antennas = _steady_receivers()
    world = World(antennas, seed=5)
    world.add_genuine(10, min_km=20, max_km=120)
    monitor = make_monitor(world, integrity_config(antennas))
    rng = random.Random(1)
    lag = {a.name: 0.0 for a in antennas}
    for _ in range(160):
        frames = world.step(0.5)
        extra = []
        for f in frames:
            for k in (1, 2):
                dt = 0.15 * k + 0.1 * k
                extra.append(
                    RawFrame(
                        _steady_velocity(f.hex[2:8]),
                        f.timestamp + dt,
                        f.receiver,
                        f.rssi,
                        (f.clock + int(dt * BEAST_CLOCK_HZ)) % BEAST_CLOCK_MOD,
                    )
                )
        for name in lag:
            lag[name] = rng.uniform(0, 0.05)
        kept = [
            RawFrame(f.hex, f.timestamp + lag[f.receiver], f.receiver, f.rssi, f.clock)
            for f in frames + extra
            if rng.random() < 0.5
        ]
        monitor.observe_frames(kept)
        monitor.evaluate(world.planes, world.t, force=True)
    worst = max(abs(r) for ev in monitor._evidence.values() for _t, r in ev.tdoa_resid)
    assert worst < 50_000, worst
    for icao in world.planes:
        assert integrity_of(world, icao)["checks"]["tdoa"]["status"] == "pass"


def test_impossible_pair_offsets_are_not_residuals():
    """An implied path difference longer than the baseline cannot be one
    transmission, whatever produced it."""
    sync = ClockSync()
    antennas = {
        "a": geodesy.ecef(*HOME, 100),
        "b": geodesy.ecef(*geodesy.destination(*HOME, 80, 40_000), 100),
    }
    target = (*geodesy.destination(*HOME, 10, 50_000), 9000.0)
    for i in range(40):
        sync.add_sync(
            TdoaObservation(1000.0 + i * 0.5, {"a": 1_000_000, "b": 1_000_000}),
            target,
            antennas,
            f"ac{i % 3}",
        )
    sync.refit(1020.0)
    model = sync.synced("a", "b", 1500)
    assert model is not None
    offset = model.predict(1020.0)
    good = TdoaObservation(1020.0, {"a": 1_000_000, "b": 1_000_000})
    assert len(sync.residuals_m(good, target, antennas, 1500)) == 1
    mispaired = TdoaObservation(
        1020.0, {"a": 1_000_000 + int(0.4 * BEAST_CLOCK_HZ), "b": 1_000_000}
    )
    assert sync.residuals_m(mispaired, target, antennas, 1500) == []
    assert offset == model.predict(1020.0)


# --- clock fits reject a lying aircraft as a whole ---------------------------------


def _pair_with(offsets: dict[str, float]):
    from pyaerial.integrity.tdoa import METRES_PER_TICK, PairModel

    model = PairModel("a", "b")
    for i in range(60):
        icao = list(offsets)[i % len(offsets)]
        model.add(1000.0 + i * 0.1, 1e6 + offsets[icao] / METRES_PER_TICK, icao)
    model.refit(1006.0)
    return model, METRES_PER_TICK


def test_clock_fit_ignores_one_aircraft_far_from_the_rest():
    """Point-wise outlier rejection keeps a whole aircraft's cluster when it is
    a quarter of the points (a spoofer trusted at start-up)."""
    model, mpt = _pair_with({"g1": 0.0, "g2": 5.0, "g3": -5.0, "spoof": 30_000.0})
    assert model.fitted
    assert abs(model.predict(1006.0) - 1e6) * mpt < 20
    assert model.spread_m < 20


def test_clock_fit_refuses_two_aircraft_that_disagree():
    model, _mpt = _pair_with({"g1": 0.0, "spoof": 30_000.0})
    assert not model.fitted


def test_spoofer_among_few_genuine_aircraft_at_start_up():
    """Four genuine aircraft and a spoofer earn trust together; the spoofer's
    sync points must neither reset receiver pairs nor skew the emitter fix."""
    from pyaerial.integrity import tdoa as tdoa_module

    resets = []
    real_reset = tdoa_module.PairModel.reset

    def counting_reset(self):
        resets.append((self.a, self.b))
        real_reset(self)

    tdoa_module.PairModel.reset = counting_reset
    try:
        antennas = _steady_receivers()
        world = World(antennas, seed=6)
        world.add_genuine(4, min_km=25, max_km=120)
        spoof = add_spoofer(world, "b20001", emitter_km=8, claim_km=90)
        monitor = make_monitor(world, integrity_config(antennas))
        worst = 0.0
        for _ in range(int(50 / 0.05)):
            monitor.observe_frames(with_velocity(world.step(0.05)))
            monitor.evaluate(world.planes, world.t)
            cached = monitor._cached.get("B20001")
            if cached and cached[1]:
                em = cached[1]
                worst = max(
                    worst,
                    geodesy.ground_distance_km(em["latitude"], em["longitude"], *spoof.emitter[:2]),
                )
    finally:
        tdoa_module.PairModel.reset = real_reset
    assert resets == []
    assert 0 < worst < 3


# --- clock-sync slots -----------------------------------------------------------------


def test_off_phase_aircraft_contribute_as_many_sync_points():
    """Aircraft scored between refits fed points older than the last admitted
    slot, which were dropped: half the points of on-phase aircraft."""
    model = PairModel("a", "b")
    obs = {
        "A": [i * 0.25 for i in range(400)],
        "B": [i * 0.25 + 0.1 for i in range(400)],
    }
    cursor = {"A": -1.0, "B": -1.0}
    for k in range(160):
        now = k * 0.5
        for ac in ["A"] if k % 2 == 0 else ["B"]:
            for t in obs[ac]:
                if cursor[ac] < t <= now:
                    model.add(t, 1000.0, ac)
            cursor[ac] = now
        if k % 2 == 0:
            model.refit(now)
    admitted = Counter(p[2] for p in model.points)
    assert model.fitted
    assert abs(admitted["A"] - admitted["B"]) <= 0.15 * max(admitted.values()), admitted
    times = [p[0] for p in model.points]
    assert times == sorted(times)
    assert len({int(t // (30.0 / 400)) for t in times}) == len(times)  # one per slot
