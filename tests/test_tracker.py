from __future__ import annotations

import pytest

from helpers import (
    IDENT,
    airborne_position,
    latest_position,
    make_config,
    tracker_at_home,
    with_crc,
)
from pyaerial.classify import (
    CAT_AIRBORNE_BARO,
    CAT_AIRBORNE_GNSS,
    CAT_IDENT,
    CAT_SURFACE,
    CAT_VELOCITY,
    classify,
)
from pyaerial.config.schema import HomeConfig
from pyaerial.constants import (
    STORE_ALT,
    STORE_GNSS_HEIGHT,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState
from pyaerial.receivers.frames import es_icao
from pyaerial.tracker import Tracker, _format_packet_types
from pyaerial.units import FT_TO_M


def test_typecode_minus_one_is_ignored():
    home = HomeConfig(latitude=35.7, longitude=-78.7)
    # DF17 with an invalid/unknown typecode path: classify returns None for -1.
    assert classify("00000000000000", home) is None


def test_unchanged_value_refreshes_timestamp():
    tracker = Tracker(make_config())
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(
            first_packet=100.0, last_update=100.0, packets=1, packet_types={}
        ),
        received_data={STORE_LAT: [Datum(35.7, 100.0)]},
    )
    tracker.planes["abc123"] = plane

    from pyaerial.classify import ClassifiedMessage

    classified = ClassifiedMessage(
        icao="abc123",
        typecode_category=3,
        values={STORE_LAT: 35.7},
    )
    tracker._merge(classified, 150.0)
    series = plane.received_data[STORE_LAT]
    assert len(series) == 2
    assert series[0].time == 100.0
    assert series[1].time == 150.0
    assert series[0].value == series[1].value == 35.7


def test_surface_without_altitude_does_not_clobber_baro():
    tracker = Tracker(make_config())
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(
            first_packet=100.0, last_update=100.0, packets=1, packet_types={}
        ),
        received_data={
            STORE_LAT: [Datum(35.7, 100.0)],
            "altitude": [Datum(400.0, 100.0)],
        },
    )
    tracker.planes["abc123"] = plane

    from pyaerial.classify import ClassifiedMessage
    from pyaerial.constants import STORE_ALT

    classified = ClassifiedMessage(
        icao="abc123",
        typecode_category=2,
        values={STORE_LAT: 35.7, STORE_LONG: -78.7},
    )
    tracker._merge(classified, 150.0)
    alt_series = plane.received_data[STORE_ALT]
    assert alt_series[-1].value == 400.0


def test_value_change_appends():
    tracker = Tracker(make_config())
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(
            first_packet=100.0, last_update=100.0, packets=1, packet_types={}
        ),
        received_data={STORE_LAT: [Datum(35.7, 100.0)]},
    )
    tracker.planes["abc123"] = plane

    from pyaerial.classify import ClassifiedMessage

    classified = ClassifiedMessage(
        icao="abc123",
        typecode_category=3,
        values={STORE_LAT: 35.71},
    )
    tracker._merge(classified, 150.0)
    series = plane.received_data[STORE_LAT]
    assert len(series) == 2
    assert series[-1].value == 35.71


def test_older_packet_does_not_rewind_last_update():
    tracker = Tracker(make_config())
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(
            first_packet=100.0, last_update=150.0, packets=1, packet_types={}
        ),
        received_data={STORE_LAT: [Datum(35.7, 150.0)]},
    )
    tracker.planes["abc123"] = plane
    from pyaerial.classify import ClassifiedMessage

    classified = ClassifiedMessage(
        icao="abc123",
        typecode_category=3,
        values={STORE_LAT: 35.71},
    )
    tracker._merge(classified, 120.0)
    assert plane.track.last_update == 150.0
    series = plane.received_data[STORE_LAT]
    assert len(series) == 1
    assert series[0].value == 35.7
    assert series[0].time == 150.0


def test_telemetry_series_are_not_clipped_to_live_window():
    from pyaerial.classify import ClassifiedMessage
    from pyaerial.config.schema import TrackingConfig

    tracker = Tracker(make_config(tracking=TrackingConfig(telemetry_keep_seconds=5)))
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(
            first_packet=100.0, last_update=110.0, packets=3, packet_types={}
        ),
        received_data={
            STORE_LAT: [Datum(35.7, 100.0), Datum(35.71, 101.0), Datum(35.72, 110.0)]
        },
    )
    tracker.planes["abc123"] = plane
    classified = ClassifiedMessage(
        icao="abc123",
        typecode_category=3,
        values={STORE_LAT: 35.73},
    )
    tracker._merge(classified, 112.0)
    series = plane.received_data[STORE_LAT]
    assert [item.time for item in series] == [100.0, 101.0, 110.0, 112.0]


def test_blank_callsign_does_not_clobber_existing():
    from pyaerial.classify import ClassifiedMessage

    tracker = Tracker(make_config())
    plane = Plane(
        info=AircraftInfo(icao="abc123", callsign="SWA123"),
        track=TrackState(
            first_packet=100.0, last_update=100.0, packets=1, packet_types={}
        ),
        received_data={},
    )
    tracker.planes["abc123"] = plane
    classified = ClassifiedMessage(
        icao="abc123",
        typecode_category=1,
        values={},
        callsign="",
    )
    tracker._merge(classified, 110.0)
    assert plane.info.callsign == "SWA123"


def test_one_bad_fix_does_not_freeze_the_plane_forever():
    """Regression: a single bad accepted fix made the 0.5 deg gate reject every
    later (correct) position for the rest of the flight."""
    config = make_config()
    config.home.latitude = 52.25
    config.home.longitude = 3.9
    tracker = Tracker(config)
    bad = (52.2572 + 0.6, 3.9194)
    tracker.planes["40621D"] = Plane(
        info=AircraftInfo(icao="40621D"),
        track=TrackState(
            first_packet=100.0, last_update=100.0, packets=1, packet_types={}
        ),
        received_data={
            STORE_LAT: [Datum(bad[0], 100.0)],
            STORE_LONG: [Datum(bad[1], 100.0)],
        },
    )
    msg = "8D40621D58C382D690C8AC2863A7"  # true fix ~ (52.2572, 3.9194)
    for n in range(5):
        assert tracker.ingest([(msg, 101.0 + n * 0.1)]) == 0
    assert tracker._gate_rejects["40621D"] == 5
    # The reset re-anchors from a global even/odd decode.
    odd = "8D40621D58C386435CC412692AD6"  # ~ (52.2658, 3.9389)
    assert tracker.ingest([(odd, 102.0)]) == 1
    lat = tracker.planes["40621D"].received_data[STORE_LAT][-1].value
    assert abs(lat - 52.2658) < 0.001
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


def test_first_fix_decodes_from_even_odd_pair_far_from_home():
    from pyaerial.config.schema import ReceiverConfig, ReceiverPositionConfig
    from pyaerial.receivers.positions import ReceiverPositions

    # Reference airborne pair near 52.25N 3.92E (pyModeS docs). A home in
    # North Carolina is far outside the 180 NM local-decode range; the global
    # even/odd decode needs no reference at all.
    config = make_config(
        receivers={
            "nl": ReceiverConfig(
                type="dump1090",
                position=ReceiverPositionConfig(latitude=52.0, longitude=4.0),
            )
        }
    )
    tracker = Tracker(config, positions=ReceiverPositions(config, fetch=False))
    even = "8d40621d58c382d690c8ac2863a7"
    odd = "8d40621d58c386435cc412692ad6"
    tracker.ingest([(even, 1.0)], receivers={even: ("nl", "be")})
    assert "40621D" not in tracker.planes  # one frame is ambiguous
    tracker.ingest([(odd, 1.5)], receivers={odd: ("nl", "be")})
    plane = tracker.planes["40621D"]
    lat = plane.received_data[STORE_LAT][-1].value
    lon = plane.received_data[STORE_LONG][-1].value
    assert abs(lat - 52.2658) < 0.001
    assert abs(lon - 3.9389) < 0.001
    assert plane.track.position_receivers == {1.5: ("nl", "be")}
    assert plane.info.receivers == ["nl", "be"]


# --- CRC, DF18 and TIS-B filtering -------------------------------------------
# Corrupted or non-ICAO extended squitters never create or update a plane.


def _df18(cf: int, me_hex: str = IDENT[8:22], icao: str = "4840d6") -> str:
    return with_crc(f"{(18 << 3) | cf:02x}{icao}{me_hex}000000")


# --- CRC and DF18 filtering --------------------------------------------------


def test_corrupted_extended_squitter_creates_no_plane():
    corrupted = IDENT[:-1] + ("9" if IDENT[-1] != "9" else "8")
    assert es_icao(corrupted) is None
    # Bit error in the address field: would otherwise create a ghost aircraft.
    ghost = IDENT[:2] + "4841d6" + IDENT[8:]
    tracker = Tracker(make_config())
    assert tracker.ingest([(corrupted, 1.0), (ghost, 1.0)]) == 0
    assert tracker.planes == {}
    assert tracker.ingest([(IDENT, 2.0)]) == 1
    assert list(tracker.planes) == ["4840D6"]


def test_df18_without_icao_address_or_standard_me_is_ignored():
    # CF 0 (ADS-B NT), 2 (fine TIS-B, ICAO), 6 (ADS-R, ICAO) carry ICAO addresses.
    for cf in (0, 2, 6):
        assert es_icao(_df18(cf)) == "4840D6", cf
    # CF 1 / 5 use non-ICAO addresses; 3 is coarse TIS-B, 4 management, 7 reserved.
    for cf in (1, 3, 4, 5, 7):
        assert es_icao(_df18(cf)) is None, cf


def test_tisb_imf_flag_marks_non_icao_address():
    lat, lon = 35.8, -78.7
    first = (18 << 3) | 2
    icao_addressed = airborne_position("a1b2c3", lat, lon, 0, first_byte=first)
    track_number = airborne_position("a1b2c3", lat, lon, 0, first_byte=first, imf=1)
    assert es_icao(icao_addressed) == "A1B2C3"
    assert es_icao(track_number) is None


def _velocity(icao: str, speed_kts: int = 400) -> str:
    """DF17 TC19 subtype 1 velocity, due north, with a valid CRC."""
    me = (19 << 51) | (1 << 48) | (1 << 32) | ((speed_kts + 1) << 21) | (1 << 10)
    return with_crc(f"8d{icao}{me:014x}000000")


def test_gate_reset_is_not_used_up_by_a_velocity_frame():
    """Regression: after five rejected fixes any next frame (usually velocity)
    consumed the reset, so the next position was gated against the bad fix
    again and the plane could stay frozen."""
    tracker = tracker_at_home()
    t = 1.0
    for i in range(2):
        msg = airborne_position("a1b2c3", 36.0, -78.5, i % 2)
        tracker.ingest([(msg, t)])
        t += 0.5
    assert abs(latest_position(tracker, "A1B2C3")[0] - 36.0) < 0.001
    # The aircraft really is 0.6 deg north (e.g. the first fix was a ghost).
    for i in range(5):
        msg = airborne_position("a1b2c3", 36.6, -78.5, i % 2)
        assert tracker.ingest([(msg, t)]) == 0
        t += 0.1
    assert tracker.ingest([(_velocity("a1b2c3"), t)]) == 1
    msg = airborne_position("a1b2c3", 36.6, -78.5, 1)
    assert tracker.ingest([(msg, t + 0.1)]) == 1
    assert abs(latest_position(tracker, "A1B2C3")[0] - 36.6) < 0.001


# --- Tracker bookkeeping --------------------------------------------------------


def test_expired_planes_honours_explicit_zero_time():
    tracker = Tracker(make_config())
    tracker.ingest([(IDENT, 1_000.0)])
    assert tracker.planes["4840D6"].track.last_update == 1_000.0
    # 0.0 is an explicit time, not "now": nothing is older than it.
    assert tracker.expired_planes(0.0) == []
    assert tracker.expired_planes(1_000.0 + 10_000) == ["4840D6"]


def test_position_receivers_only_kept_for_stored_samples():
    """A repeated identical fix keeps a two-sample run, but its receivers map
    grew by one entry per frame for as long as it was heard."""
    tracker = tracker_at_home()
    tracker.ingest([(airborne_position("a1b2c3", 36.0, -78.5, 1), 0.0)])
    t = 0.5
    msg = airborne_position("a1b2c3", 36.0, -78.5, 0)
    for _ in range(400):
        tracker.ingest([(msg, t)], receivers={msg: ("nc",)})
        t += 0.5
    plane = tracker.planes["A1B2C3"]
    recv = plane.received_data
    sample_times = {d.time for d in recv[STORE_LAT]} | {
        d.time for d in recv[STORE_LONG]
    }
    receivers = plane.track.position_receivers
    assert set(receivers) == sample_times
    assert len(receivers) < 10


def test_out_of_order_position_does_not_leave_receivers_entry():
    tracker = tracker_at_home()
    first = airborne_position("a1b2c3", 36.0, -78.5, 0)
    second = airborne_position("a1b2c3", 36.001, -78.5, 1)
    late = airborne_position("a1b2c3", 36.0005, -78.5, 0)
    for msg, t in ((first, 1.0), (second, 1.5), (late, 1.2)):
        tracker.ingest([(msg, t)], receivers={msg: ("nc",)})
    receivers = tracker.planes["A1B2C3"].track.position_receivers
    assert set(receivers) == {1.5}


# --- GNSS height is kept apart from baro altitude ----------------------------


HOME = HomeConfig(latitude=35.7275, longitude=-78.6959)


# --- GNSS height is not barometric altitude ----------------------------------


def test_gnss_height_is_stored_apart_from_baro_altitude():
    gnss = airborne_position("a1b2c3", 36.0, -78.5, 0, alt_ft=36000, tc=20)
    result = classify(gnss, HOME, last_position=(36.0, -78.5))
    assert result is not None
    recv = result.values
    assert STORE_ALT not in recv
    # Same AC12 25-ft coding as baro (dump1090, readsb, rs1090), not metres.
    assert recv[STORE_GNSS_HEIGHT] == pytest.approx(36000 * FT_TO_M)

    baro = airborne_position("a1b2c3", 36.0, -78.5, 0, alt_ft=35000, tc=11)
    recv = classify(baro, HOME, last_position=(36.0, -78.5)).values
    assert recv[STORE_ALT] == pytest.approx(35000 * FT_TO_M)
    assert STORE_GNSS_HEIGHT not in recv


def test_gnss_positions_do_not_enter_the_baro_altitude_series():
    tracker = Tracker(make_config())
    t = 1.0
    for i in range(6):
        tc = 11 if i % 3 else 20
        alt = 35000 if tc == 11 else 35600
        msg = airborne_position("a1b2c3", 36.0 + i * 0.001, -78.5, i % 2, alt_ft=alt, tc=tc)
        tracker.ingest([(msg, t)])
        t += 0.5
    recv = tracker.planes["A1B2C3"].received_data
    assert {round(d.value) for d in recv[STORE_ALT]} == {round(35000 * FT_TO_M)}
    assert {round(d.value) for d in recv[STORE_GNSS_HEIGHT]} == {round(35600 * FT_TO_M)}


# --- Status-line packet-type summary -----------------------------------------


@pytest.mark.parametrize(
    "counts, text",
    [
        ({CAT_AIRBORNE_BARO: 81, CAT_VELOCITY: 2, CAT_IDENT: 2}, "pos 81, vel 2, id 2"),
        ({CAT_SURFACE: 0, CAT_AIRBORNE_GNSS: 3}, "gnss pos 3"),  # zero omitted
        ({}, ""),
    ],
)
def test_packet_types_are_formatted_readably(counts, text):
    assert _format_packet_types(counts) == text


def test_status_line_has_no_defaultdict_repr():
    tracker = Tracker(make_config())
    tracker.ingest([(IDENT, 1.0)])
    summary = tracker.top_planes_summary()
    assert "defaultdict" not in summary
    assert summary == "Top 1: 4840D6/KLM1023 (1, id 1)"
    assert tracker.planes["4840D6"].track.packet_types
