from __future__ import annotations

import math

from pyaerial.classify import classify
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


def test_groundspeed_stored_as_kmh():
    home = HomeConfig(latitude=35.7275, longitude=-78.6959)
    # 100 kt encoded → classify should store ~185.2 km/h
    msg = _encode_velocity("ABC123", 185.2, 90.0)
    result = classify(msg, home)
    assert result is not None
    speed = result.data[STORE_RECV_DATA].get(STORE_HORIZ_SPEED)
    assert speed is not None
    assert 180.0 < speed < 190.0
