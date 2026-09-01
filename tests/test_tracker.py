from __future__ import annotations

from pyaerial.classify import classify
from pyaerial.config.schema import HomeConfig
from pyaerial.constants import (
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_MOST_RECENT_PACKET,
    STORE_PACKET_TYPE,
    STORE_RECV_DATA,
    STORE_TOTAL_PACKETS,
)
from pyaerial.models import Datum
from pyaerial.tracker import Tracker
from helpers import make_config


def test_typecode_minus_one_is_ignored():
    home = HomeConfig(latitude=35.7, longitude=-78.7)
    # DF17 with an invalid/unknown typecode path: classify returns None for -1.
    assert classify("00000000000000", home) is None


def test_unchanged_value_refreshes_timestamp():
    tracker = Tracker(make_config())
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {STORE_LAT: [Datum(35.7, 100.0)]},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 100.0,
            STORE_MOST_RECENT_PACKET: 100.0,
            STORE_TOTAL_PACKETS: 1,
            STORE_PACKET_TYPE: {},
        },
    }
    tracker.planes["abc123"] = plane

    from pyaerial.classify import ClassifiedMessage

    classified = ClassifiedMessage(
        data={
            STORE_INFO: {STORE_ICAO: "abc123"},
            STORE_RECV_DATA: {STORE_LAT: 35.7},
        },
        typecode_category=3,
    )
    tracker._merge(classified, 150.0)
    series = plane[STORE_RECV_DATA][STORE_LAT]
    assert len(series) == 1
    assert series[0].time == 150.0


def test_value_change_appends():
    tracker = Tracker(make_config())
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {STORE_LAT: [Datum(35.7, 100.0)]},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 100.0,
            STORE_MOST_RECENT_PACKET: 100.0,
            STORE_TOTAL_PACKETS: 1,
            STORE_PACKET_TYPE: {},
        },
    }
    tracker.planes["abc123"] = plane

    from pyaerial.classify import ClassifiedMessage

    classified = ClassifiedMessage(
        data={
            STORE_INFO: {STORE_ICAO: "abc123"},
            STORE_RECV_DATA: {STORE_LAT: 35.71},
        },
        typecode_category=3,
    )
    tracker._merge(classified, 150.0)
    series = plane[STORE_RECV_DATA][STORE_LAT]
    assert len(series) == 2
    assert series[-1].value == 35.71


def test_telemetry_series_are_capped():
    from pyaerial.config.schema import TrackingConfig
    from pyaerial.classify import ClassifiedMessage

    tracker = Tracker(make_config(tracking=TrackingConfig(telemetry_keep_seconds=5)))
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.7, 100.0), Datum(35.71, 101.0), Datum(35.72, 110.0)]
        },
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 100.0,
            STORE_MOST_RECENT_PACKET: 110.0,
            STORE_TOTAL_PACKETS: 3,
            STORE_PACKET_TYPE: {},
        },
    }
    tracker.planes["abc123"] = plane
    classified = ClassifiedMessage(
        data={
            STORE_INFO: {STORE_ICAO: "abc123"},
            STORE_RECV_DATA: {STORE_LAT: 35.73},
        },
        typecode_category=3,
    )
    tracker._merge(classified, 112.0)
    series = plane[STORE_RECV_DATA][STORE_LAT]
    assert all(item.time >= 107.0 for item in series)
