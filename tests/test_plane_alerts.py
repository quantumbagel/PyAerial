from __future__ import annotations

import time

from pyaerial.calc.geo import build_polygons
from pyaerial.calc.plane import PlaneCalculator
from pyaerial.constants import (
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum
from helpers import make_config


def _plane(lat: float = 35.725, lon: float = -78.695, t: float | None = None):
    if t is None:
        t = time.time()
    return {
        STORE_INFO: {STORE_ICAO: "abc123", "callsign": "N123AB"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(lat, t - 5), Datum(lat + 0.001, t)],
            STORE_LONG: [Datum(lon, t - 5), Datum(lon, t)],
            STORE_ALT: [Datum(400.0, t)],
        },
        STORE_CALC_DATA: {},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: t - 5,
            STORE_MOST_RECENT_PACKET: t,
        },
    }


def test_while_active_starts_at_activation_time():
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        plane = _plane()
        calc.calculate_plane(plane)
        keys = [k for k in calc._alert_state if k[0] == "abc123"]
        assert keys, "expected an active alert for a plane inside the zone"
        state = calc._alert_state[keys[0]]
        assert state["last_periodic"] == state["activated_at"]
        assert state["last_periodic"] > 0
    finally:
        calc.close()


def test_forget_motion_drops_kalman():
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        plane = _plane()
        calc.calculate_plane(plane)
        assert "abc123" in calc._kalman_filters
        calc.forget_motion("abc123")
        assert "abc123" not in calc._kalman_filters
        assert "abc123" not in calc._smoothed_turn_rates
    finally:
        calc.close()


def test_deactivate_clears_motion_and_alerts():
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        plane = _plane()
        calc.calculate_plane(plane)
        calc.deactivate_plane(plane)
        assert plane.get("active_alerts") == []
        assert "abc123" not in calc._kalman_filters
    finally:
        calc.close()


def test_single_position_still_calculates():
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        t = 1_700_000_000.0
        plane = {
            STORE_INFO: {STORE_ICAO: "abc123"},
            STORE_RECV_DATA: {
                STORE_LAT: [Datum(35.725, t)],
                STORE_LONG: [Datum(-78.695, t)],
            },
            STORE_CALC_DATA: {},
            STORE_INTERNAL: {
                STORE_FIRST_PACKET: t,
                STORE_MOST_RECENT_PACKET: t,
            },
        }
        calc.calculate_plane(plane)  # must not raise
    finally:
        calc.close()
