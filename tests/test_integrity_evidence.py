"""What the integrity monitor accepts as evidence and how much it keeps:
frame validity, DF18/TIS-B, evidence windows and bounded memory."""

from __future__ import annotations

from dataclasses import replace

import pytest

from integrity_world import (
    HOME,
    Aircraft,
    Antenna,
    World,
    add_spoofer,
    es_position,
    integrity_config,
    integrity_of,
    make_monitor,
    with_crc,
    with_velocity,
)
from pyaerial.integrity import STATUS_OK, geodesy
from pyaerial.integrity.checks import FAIL
from pyaerial.integrity.evidence import TIMING_MAX_SAMPLES, series_window
from pyaerial.models import Datum
from pyaerial.receivers.frames import RawFrame

# --- Evidence windows and what is kept ---------------------------------------


def test_series_window_keeps_spanning_run_only():
    series = [Datum(1.0, 0.0), Datum(1.0, 10.0), Datum(2.0, 11.0), Datum(3.0, 20.0)]
    assert [d.value for d in series_window(series, 5.0, 9.0)] == [1.0]
    # A value change, not a run: the old value is not carried forward.
    assert [d.value for d in series_window(series, 12.0, 21.0)] == [3.0]
    assert series_window(series, 25.0, 30.0) == []


def test_one_tis_b_frame_does_not_exempt_direct_transmissions():
    """Only rebroadcast-only targets skip the RF checks; a spoofer sending one
    DF18 TIS-B frame per target must not switch them off."""
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(15)
    spoof = add_spoofer(world, "b40001")
    monitor = make_monitor(world, integrity_config(antennas))
    for i in range(80):
        frames = world.step(0.5)
        if i == 10:
            frames += [
                replace(f, hex=with_crc("92" + f.hex[2:]))
                for f in frames
                if f.hex[2:8] == spoof.icao
            ][:1]
        monitor.observe_frames(frames)
        monitor.evaluate(world.planes, world.t, force=True)
    doc = integrity_of(world, "b40001")
    assert "rebroadcast" not in doc
    assert doc["checks"]["rssi_range"]["status"] == "fail"
    assert doc["status"] != STATUS_OK


def test_disabled_monitor_keeps_no_evidence():
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(3)
    monitor = make_monitor(world, integrity_config(antennas, enabled=False))
    for _ in range(20):
        monitor.observe_frames(world.step(0.5))
        monitor.evaluate(world.planes, world.t, force=True)
    assert monitor._evidence == {}


# --- frame validity -----------------------------------------------------------


def _df18(icao: str, cf: int, *, imf: bool = False) -> str:
    me = int(es_position(icao, 0)[8:22], 16) | (int(imf) << 48)  # IMF: ME bit 8
    return with_crc(f"{(18 << 3) | cf:02x}{icao.lower()}{me:014x}000000")


def _observe(hex_msg: str):
    antennas = [Antenna("a", *HOME)]
    monitor = make_monitor(World(antennas), integrity_config(antennas))
    monitor.observe_frames([RawFrame(hex_msg, 1000.0, "a", -10.0, 12345)])
    return monitor._evidence


def test_corrupted_extended_squitter_creates_no_evidence():
    good = es_position("a10000", 0)
    assert set(_observe(good)) == {"A10000"}
    # One bit error in the address: parity no longer checks out.
    bad = good[:7] + f"{int(good[7], 16) ^ 1:x}" + good[8:]
    assert _observe(bad) == {}


@pytest.mark.parametrize(("cf", "rebroadcast"), [(0, False), (2, True), (6, True)])
def test_df18_with_icao_address(cf, rebroadcast):
    ev = _observe(_df18("a10000", cf))["A10000"]
    assert ev.rebroadcast is rebroadcast
    # Only direct transmissions describe the aircraft's own transmitter.
    assert bool(ev.rssi) is not rebroadcast


@pytest.mark.parametrize(
    ("cf", "imf"),
    [(1, False), (2, True), (3, False), (4, False), (5, False), (6, True), (7, False)],
)
def test_df18_without_icao_address_is_ignored(cf, imf):
    assert _observe(_df18("a10000", cf, imf=imf)) == {}


def _four_receivers() -> list[Antenna]:
    return [
        Antenna("a", *HOME, clock_offset=12.3, drift_ppm=3),
        Antenna("b", *geodesy.destination(*HOME, 80, 40_000), clock_offset=-401.7),
        Antenna("c", *geodesy.destination(*HOME, 200, 45_000), clock_offset=77.0),
        Antenna("d", *geodesy.destination(*HOME, 330, 35_000), clock_offset=5.5),
    ]


@pytest.mark.parametrize("window", [60.0, 120.0, 300.0])
def test_evidence_spans_window_seconds(window):
    antennas = _four_receivers()
    world = World(antennas, seed=11)
    world.add_genuine(8, min_km=20, max_km=120)
    monitor = make_monitor(world, integrity_config(antennas, window_seconds=window))
    for _ in range(int(window) + 40):
        monitor.observe_frames(with_velocity(world.step(1.0)))
        monitor.evaluate(world.planes, world.t, force=True)
    ev = monitor._evidence["A10000"]
    for receiver in "abcd":
        rows = ev.rssi_geo[receiver]
        assert rows[-1][0] - rows[0][0] >= window - 5
        assert len(rows) < rows.maxlen
        assert not ev.rssi[receiver]  # raw samples dropped once resolved
    # TDOA is judged over the last 30 s; all six pairs, the whole 30 s.
    assert ev.tdoa_resid[-1][0] - ev.tdoa_resid[0][0] >= 25
    assert len(ev.tdoa_resid) < ev.tdoa_resid.maxlen
    assert len(ev.tdoa) <= 64


def test_flooding_transmitter_is_bounded():
    antennas = _four_receivers()
    world = World(antennas)
    world.add_genuine(1)
    monitor = make_monitor(world, integrity_config(antennas))
    frame = es_position("a10000", 0)
    for second in range(150):
        t = world.t + second
        monitor.observe_frames(
            [RawFrame(frame, t + i / 1000, "a", -10.0, i) for i in range(1000)]
        )
        monitor.evaluate(world.planes, t, force=True)
    ev = monitor._evidence["A10000"]
    assert len(ev.rssi["a"]) <= ev.rssi["a"].maxlen <= 64
    assert len(ev.rssi_geo["a"]) <= ev.rssi_geo["a"].maxlen
    assert len(ev.pos_clock["a"]) <= ev.pos_clock["a"].maxlen <= TIMING_MAX_SAMPLES


def test_rssi_trend_sees_a_fixed_transmitter_over_the_whole_window():
    """A spoofer claiming to fly from 15 to 45 km away at 900 km/h: only a
    window of about 120 s spans the range change rssi_trend needs."""
    antennas = [Antenna("a", *HOME)]
    world = World(antennas)
    world.add_genuine(10)
    lat, lon = geodesy.destination(HOME[0], HOME[1], 45, 15_000)
    elat, elon = geodesy.destination(HOME[0], HOME[1], 200, 30_000)
    world.add(Aircraft("b50001", lat, lon, 5000, 900, 45, emitter=(elat, elon, 50.0)))
    monitor = make_monitor(world, integrity_config(antennas))
    for _ in range(130):
        monitor.observe_frames(with_velocity(world.step(1.0)))
        monitor.evaluate(world.planes, world.t, force=True)
    assert integrity_of(world, "b50001")["checks"]["rssi_trend"]["status"] == FAIL
    assert (
        integrity_of(world, "a10000")["checks"].get("rssi_trend", {}).get("status")
        != FAIL
    )
