from __future__ import annotations

from pyaerial.classify import classify
from pyaerial.config.schema import HomeConfig
from pyaerial.constants import (
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_PACKET_TYPE,
    STORE_POSITION_RECEIVERS,
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
    assert len(series) == 2
    assert series[0].time == 100.0
    assert series[1].time == 150.0
    assert series[0].value == series[1].value == 35.7


def test_surface_without_altitude_does_not_clobber_baro():
    tracker = Tracker(make_config())
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {STORE_LAT: [Datum(35.7, 100.0)], "altitude": [Datum(400.0, 100.0)]},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 100.0,
            STORE_MOST_RECENT_PACKET: 100.0,
            STORE_TOTAL_PACKETS: 1,
            STORE_PACKET_TYPE: {},
        },
    }
    tracker.planes["abc123"] = plane

    from pyaerial.classify import ClassifiedMessage
    from pyaerial.constants import STORE_ALT

    classified = ClassifiedMessage(
        data={
            STORE_INFO: {STORE_ICAO: "abc123"},
            STORE_RECV_DATA: {STORE_LAT: 35.7, STORE_LONG: -78.7},
        },
        typecode_category=2,
    )
    tracker._merge(classified, 150.0)
    alt_series = plane[STORE_RECV_DATA][STORE_ALT]
    assert alt_series[-1].value == 400.0


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


def test_older_packet_does_not_rewind_last_update():
    tracker = Tracker(make_config())
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {STORE_LAT: [Datum(35.7, 150.0)]},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 100.0,
            STORE_MOST_RECENT_PACKET: 150.0,
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
    tracker._merge(classified, 120.0)
    assert plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET] == 150.0
    series = plane[STORE_RECV_DATA][STORE_LAT]
    assert len(series) == 1
    assert series[0].value == 35.7
    assert series[0].time == 150.0


def test_telemetry_series_are_not_clipped_to_live_window():
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
    assert [item.time for item in series] == [100.0, 101.0, 110.0, 112.0]


def test_blank_callsign_does_not_clobber_existing():
    from pyaerial.classify import ClassifiedMessage

    tracker = Tracker(make_config())
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123", STORE_CALLSIGN: "SWA123"},
        STORE_RECV_DATA: {},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 100.0,
            STORE_MOST_RECENT_PACKET: 100.0,
            STORE_TOTAL_PACKETS: 1,
            STORE_PACKET_TYPE: {},
        },
    }
    tracker.planes["abc123"] = plane
    classified = ClassifiedMessage(
        data={
            STORE_INFO: {STORE_ICAO: "abc123", STORE_CALLSIGN: ""},
            STORE_RECV_DATA: {},
        },
        typecode_category=1,
    )
    tracker._merge(classified, 110.0)
    assert plane[STORE_INFO][STORE_CALLSIGN] == "SWA123"


def test_one_bad_fix_does_not_freeze_the_plane_forever():
    """Regression: a single bad accepted fix made the 0.5 deg gate reject every
    later (correct) position for the rest of the flight."""
    config = make_config()
    config.home.latitude = 52.25
    config.home.longitude = 3.9
    tracker = Tracker(config)
    bad = (52.2572 + 0.6, 3.9194)
    tracker.planes["40621D"] = {
        STORE_INFO: {STORE_ICAO: "40621D"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(bad[0], 100.0)],
            STORE_LONG: [Datum(bad[1], 100.0)],
        },
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 100.0,
            STORE_MOST_RECENT_PACKET: 100.0,
            STORE_TOTAL_PACKETS: 1,
            STORE_PACKET_TYPE: {},
        },
    }
    msg = "8D40621D58C382D690C8AC2863A7"  # true fix ~ (52.2572, 3.9194)
    for n in range(5):
        assert tracker.ingest([(msg, 101.0 + n * 0.1)]) == 0
    assert tracker._gate_rejects["40621D"] == 5
    assert tracker.ingest([(msg, 102.0)]) == 1
    lat = tracker.planes["40621D"][STORE_RECV_DATA][STORE_LAT][-1].value
    assert abs(lat - 52.2572) < 0.01
    assert "40621D" not in tracker._gate_rejects


def test_short_mode_s_frames_skip_the_adsb_decoder(monkeypatch):
    import pyaerial.tracker as tracker_mod

    calls: list[str] = []
    real = tracker_mod.classify

    def spy(msg, *args, **kwargs):
        calls.append(msg)
        return real(msg, *args, **kwargs)

    monkeypatch.setattr(tracker_mod, "classify", spy)
    tracker = Tracker(make_config())
    processed = tracker.ingest(
        [
            ("5d484fdea248f5", 1.0),  # DF11
            ("20001838ca3e51", 1.0),  # DF4
            ("8d4840d6202cc371c32ce0576098", 1.0),  # DF17 ident
        ]
    )
    assert processed == 1
    assert calls == ["8d4840d6202cc371c32ce0576098"]


def test_first_fix_decodes_against_receiving_antenna():
    from pyaerial.config.schema import ReceiverConfig, ReceiverPositionConfig
    from pyaerial.receivers.positions import ReceiverPositions

    # Reference airborne pair near 52.25N 3.92E (pyModeS docs). A home in
    # North Carolina is far outside the 180 NM local-decode range; the antenna
    # that heard the frame is not.
    config = make_config(
        receivers={
            "nl": ReceiverConfig(
                type="dump1090",
                position=ReceiverPositionConfig(latitude=52.0, longitude=4.0),
            )
        }
    )
    tracker = Tracker(config, positions=ReceiverPositions(config, fetch=False))
    msg = "8d40621d58c382d690c8ac2863a7"
    tracker.ingest([(msg, 1.0)], receivers={msg: ("nl", "be")})
    plane = tracker.planes["40621D"]
    lat = plane[STORE_RECV_DATA][STORE_LAT][-1].value
    assert abs(lat - 52.2572) < 0.01
    assert plane[STORE_INTERNAL][STORE_POSITION_RECEIVERS] == {1.0: ("nl", "be")}
    assert plane[STORE_INFO]["receivers"] == ["nl", "be"]
