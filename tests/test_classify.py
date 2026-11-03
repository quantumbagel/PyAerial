from __future__ import annotations

import math

import pyModeS as pms
import pytest

from helpers import with_crc
from pyaerial.classify import _plausible_fix, ac12_altitude_ft, classify
from pyaerial.config.schema import HomeConfig
from pyaerial.constants import STORE_HORIZ_SPEED, STORE_RECV_DATA
from pyaerial.units import KMH_TO_KT


def _encode_velocity(icao: str, speed_kmh: float, heading_deg: float) -> str:
    speed_kts = speed_kmh * KMH_TO_KT
    rad = math.radians(heading_deg)
    v_ew = speed_kts * math.sin(rad)
    v_ns = speed_kts * math.cos(rad)
    dir_ew = 1 if v_ew < 0 else 0
    val_ew = int(round(abs(v_ew))) + 1
    dir_ns = 1 if v_ns < 0 else 0
    val_ns = int(round(abs(v_ns))) + 1
    me = (
        (19 << 51)
        | (1 << 48)
        | (1 << 43)
        | (dir_ew << 42)
        | ((val_ew & 0x3FF) << 32)
        | (dir_ns << 31)
        | ((val_ns & 0x3FF) << 21)
    )
    return f"8D{icao.upper()}{me:014X}000000"


def test_non_adsb_typecode_is_ignored():
    home = HomeConfig(latitude=35.7275, longitude=-78.6959)
    assert classify("00000000000000", home) is None
    # Short Mode S / DF11-style frames must not raise on typecode is None.
    assert classify("0000000000000000000000000000", home) is None


def test_cpr_jump_from_last_fix_is_dropped():
    assert _plausible_fix(35.7, -78.7, (10.0, 10.0)) is False
    assert _plausible_fix(35.7, -78.7, (35.7, -78.7)) is True


def test_cpr_jump_allows_antimeridian_wrap():
    assert _plausible_fix(1.0, -179.9, (1.0, 179.9)) is True
    assert _plausible_fix(1.0, -170.0, (1.0, 179.9)) is False


def test_cpr_requires_finite_lat_and_lon():
    assert _plausible_fix(None, -78.7, None) is False
    assert _plausible_fix(35.7, None, None) is False
    assert _plausible_fix(float("nan"), -78.7, None) is False


def test_groundspeed_stored_as_kmh():
    home = HomeConfig(latitude=35.7275, longitude=-78.6959)
    # 100 kt encoded → classify should store ~185.2 km/h
    msg = _encode_velocity("ABC123", 185.2, 90.0)
    result = classify(msg, home)
    assert result is not None
    speed = result.data[STORE_RECV_DATA].get(STORE_HORIZ_SPEED)
    assert speed is not None
    assert 180.0 < speed < 190.0


def test_jump_gate_scales_with_elapsed_time():
    from pyaerial.classify import max_jump_deg
    from pyaerial.constants import MAX_CPR_JUMP_DEG

    assert max_jump_deg(None) == MAX_CPR_JUMP_DEG
    assert max_jump_deg(1.0) == MAX_CPR_JUMP_DEG
    assert max_jump_deg(600.0) > 4.0
    # 0.8 deg in 5 min is plausible; in 1 s it is not.
    assert _plausible_fix(36.5, -78.7, (35.7, -78.7), max_jump_deg(300.0)) is True
    assert _plausible_fix(36.5, -78.7, (35.7, -78.7), max_jump_deg(1.0)) is False


def test_lon_gate_widens_with_latitude():
    # 0.7 deg of longitude at 70N is ~27 km, well within a 0.5-deg-of-lat move.
    assert _plausible_fix(70.0, 10.7, (70.0, 10.0)) is True
    assert _plausible_fix(0.0, 10.7, (0.0, 10.0)) is False


# --- Local 12-bit (AC12) altitude decoding -----------------------------------


def _tc11_with_ac12(ac: int) -> str:
    me = (11 << 51) | (ac << 36)
    return with_crc(f"8da1b2c3{me:014x}000000")


def test_ac12_altitude_matches_pymodes_for_every_code():
    mismatches = []
    for ac in range(4096):
        expected = pms.decode(_tc11_with_ac12(ac)).get("altitude")
        if ac12_altitude_ft(ac) != expected:
            mismatches.append((ac, ac12_altitude_ft(ac), expected))
    assert mismatches == []


@pytest.mark.parametrize(
    "ac, feet",
    [
        (0x000, None),  # no altitude
        (0x010, -1000),  # Q=1, N=0
        (0xC38, 38000),  # 1090 MHz Riddle example (Q=1)
        (0x058, 0),
    ],
)
def test_ac12_altitude_known_values(ac, feet):
    assert ac12_altitude_ft(ac) == feet


def test_ac12_gillham_codes_step_by_100_ft():
    gillham = {ac12_altitude_ft(ac) for ac in range(4096) if not ac & 0x10}
    gillham.discard(None)
    assert gillham
    assert all(feet % 100 == 0 for feet in gillham)
