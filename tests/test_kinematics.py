from __future__ import annotations

from pyaerial.calc.kalman import KinematicKalmanFilter
from pyaerial.calc.kinematics import Kinematics
from pyaerial.constants import (
    MAX_SPEED_MPS,
    MIN_VEL_DT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.models import (
    AircraftInfo,
    Datum,
    Plane,
    TrackState,
    iter_telemetry_samples,
)
from helpers import make_config, make_plane


def test_trusted_adsb_speed_is_stamped_per_fix():
    config = make_config()
    kin = Kinematics(config)
    t = 1_700_000_000.0
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=t, last_update=t),
        received_data={
            STORE_LAT: [Datum(35.72, t)],
            STORE_LONG: [Datum(-78.70, t)],
            STORE_HORIZ_SPEED: [Datum(200.0, t)],
            STORE_HEADING: [Datum(45.0, t)],
        },
        calculated_data={},
    )
    kin.update(plane)
    plane.received_data[STORE_LAT].append(Datum(35.73, t + 1.0))
    plane.received_data[STORE_LONG].append(Datum(-78.69, t + 1.0))
    plane.track.last_update = t + 1.0
    kin.update(plane)

    speeds = plane.calculated_data[STORE_HORIZ_SPEED]
    headings = plane.calculated_data[STORE_HEADING]
    assert [item.time for item in speeds] == [t, t + 1.0]
    assert [item.time for item in headings] == [t, t + 1.0]

    samples = list(iter_telemetry_samples(plane))
    assert len(samples) == 2
    assert samples[0][4] is not None
    assert samples[1][4] is not None
    assert samples[0][0] == t
    assert samples[1][0] == t + 1.0


def test_kalman_skips_velocity_nudge_on_tiny_dt():
    kf = KinematicKalmanFilter(35.72, -78.70)
    _, _, speed, _ = kf.update(35.73, -78.69, MIN_VEL_DT / 2)
    assert kf.vn == 0.0
    assert kf.ve == 0.0
    assert speed == 0.0


def test_kalman_clamps_unphysical_speed():
    kf = KinematicKalmanFilter(35.0, -78.0)
    _, _, speed, _ = kf.update(36.0, -78.0, 0.5)
    assert speed == MAX_SPEED_MPS


def test_turn_rate_keeps_previous_on_microsecond_dt():
    from pyaerial.calc.motion import estimate_turn_rate_deg_s

    rate = estimate_turn_rate_deg_s(90.0, 89.5, 1e-6, prev_smoothed=0.0)
    assert rate == 0.0
    rate = estimate_turn_rate_deg_s(90.0, 80.0, 2.0, prev_smoothed=0.0)
    assert abs(rate) > 0.28


def test_microsecond_position_pair_does_not_seed_zero():
    config = make_config()
    kin = Kinematics(config)
    t = 1_700_000_000.0
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=t, last_update=t + 1e-6),
        received_data={
            STORE_LAT: [Datum(35.72, t), Datum(35.721, t + 1e-6)],
            STORE_LONG: [Datum(-78.70, t), Datum(-78.699, t + 1e-6)],
        },
        calculated_data={},
    )
    kin.update(plane)
    assert STORE_HORIZ_SPEED not in plane.calculated_data
    assert STORE_HEADING not in plane.calculated_data

    plane.received_data[STORE_LAT].append(Datum(35.73, t + 1.0))
    plane.received_data[STORE_LONG].append(Datum(-78.69, t + 1.0))
    kin.update(plane)
    speeds = plane.calculated_data[STORE_HORIZ_SPEED]
    assert speeds[-1].value > 1.0


def test_stale_position_is_not_coasted():
    import time

    from pyaerial.constants import MAX_COAST_SECONDS

    config = make_config()
    kin = Kinematics(config)
    now = time.time()
    stale = now - (MAX_COAST_SECONDS + 20.0)
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=stale, last_update=now),
        received_data={
            STORE_LAT: [Datum(35.72, stale), Datum(35.73, stale + 1.0)],
            STORE_LONG: [Datum(-78.70, stale), Datum(-78.69, stale + 1.0)],
            STORE_HORIZ_SPEED: [Datum(400.0, stale + 1.0)],
            STORE_HEADING: [Datum(45.0, stale + 1.0)],
        },
        calculated_data={},
    )
    update = kin.update(plane)
    assert update is not None
    assert update.alert_position == update.fix


def test_repeated_ticks_on_same_fix_do_not_advance_smoothing():
    """Regression: the speed/heading EMA advanced on every engine tick, so a
    single fix re-processed five times ratcheted speed 520 -> 733."""
    import time

    import pytest

    config = make_config()
    kin = Kinematics(config)
    t0 = time.time() - 1.0
    plane = make_plane(
        "abc123",
        t0 - 10,
        t0,
        recv={
            STORE_LAT: [Datum(35.0, t0 - 10), Datum(35.01, t0)],
            STORE_LONG: [Datum(-78.0, t0 - 10), Datum(-78.0, t0)],
            STORE_HORIZ_SPEED: [Datum(800.0, t0)],
            STORE_HEADING: [Datum(0.0, t0)],
        },
        calc={
            STORE_HORIZ_SPEED: [Datum(400.0, t0 - 10)],
            STORE_HEADING: [Datum(90.0, t0 - 10)],
        },
    )
    first = kin.update(plane)
    assert first is not None
    speeds = list(plane.calculated_data[STORE_HORIZ_SPEED])
    headings = list(plane.calculated_data[STORE_HEADING])
    assert speeds[-1].value == pytest.approx(520.0)
    for _ in range(4):
        again = kin.update(plane)
        assert again is not None
        assert again.fix == first.fix
    assert plane.calculated_data[STORE_HORIZ_SPEED] == speeds
    assert plane.calculated_data[STORE_HEADING] == headings

    # A genuinely new fix is smoothed again.
    plane.received_data[STORE_LAT].append(Datum(35.02, t0 + 1))
    plane.received_data[STORE_LONG].append(Datum(-78.0, t0 + 1))
    plane.received_data[STORE_HORIZ_SPEED].append(Datum(800.0, t0 + 1))
    kin.update(plane)
    assert plane.calculated_data[STORE_HORIZ_SPEED][-1].value == pytest.approx(604.0)
