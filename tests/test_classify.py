from __future__ import annotations

from pyaerial.classify import classify
from pyaerial.config.schema import HomeConfig
from pyaerial.constants import STORE_HORIZ_SPEED, STORE_RECV_DATA
from pyaerial.receivers.mock import encode_velocity


def test_groundspeed_stored_as_kmh():
    home = HomeConfig(latitude=35.7275, longitude=-78.6959)
    # 100 kt encoded → classify should store ~185.2 km/h
    msg = encode_velocity("ABC123", 185.2, 90.0)
    result = classify(msg, home)
    assert result is not None
    speed = result.data[STORE_RECV_DATA].get(STORE_HORIZ_SPEED)
    assert speed is not None
    assert 180.0 < speed < 190.0
