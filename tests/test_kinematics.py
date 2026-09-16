from __future__ import annotations

from pyaerial.calc.kinematics import Kinematics
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum, iter_telemetry_samples
from helpers import make_config


def test_trusted_adsb_speed_is_stamped_per_fix():
    config = make_config()
    kin = Kinematics(config)
    t = 1_700_000_000.0
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.72, t)],
            STORE_LONG: [Datum(-78.70, t)],
            STORE_HORIZ_SPEED: [Datum(200.0, t)],
            STORE_HEADING: [Datum(45.0, t)],
        },
        STORE_CALC_DATA: {},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: t,
            STORE_MOST_RECENT_PACKET: t,
        },
    }
    kin.update(plane)
    plane[STORE_RECV_DATA][STORE_LAT].append(Datum(35.73, t + 1.0))
    plane[STORE_RECV_DATA][STORE_LONG].append(Datum(-78.69, t + 1.0))
    plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET] = t + 1.0
    kin.update(plane)

    speeds = plane[STORE_CALC_DATA][STORE_HORIZ_SPEED]
    headings = plane[STORE_CALC_DATA][STORE_HEADING]
    assert [item.time for item in speeds] == [t, t + 1.0]
    assert [item.time for item in headings] == [t, t + 1.0]

    samples = list(iter_telemetry_samples(plane))
    assert len(samples) == 2
    assert samples[0][4] is not None
    assert samples[1][4] is not None
    assert samples[0][0] == t
    assert samples[1][0] == t + 1.0
