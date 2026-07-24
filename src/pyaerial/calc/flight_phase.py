"""
Flight phase classification module for PyAerial.

Classifies aircraft tracking states into standard operational phases:
TAXIING, TAKEOFF, CLIMB, CRUISE, DESCENT, APPROACH, LANDED, UNKNOWN.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger("pyaerial.calc.flight_phase")


class FlightPhase(str, Enum):
    TAXIING = "TAXIING"
    TAKEOFF = "TAKEOFF"
    CLIMB = "CLIMB"
    CRUISE = "CRUISE"
    DESCENT = "DESCENT"
    APPROACH = "APPROACH"
    LANDED = "LANDED"
    UNKNOWN = "UNKNOWN"


def classify_flight_phase(speed_knots: float | None,
                          alt_feet: float | None,
                          prev_alt_feet: float | None = None,
                          dt_seconds: float | None = None,
                          distance_to_home_nm: float | None = None) -> FlightPhase:
    """
    Classify the current flight phase based on telemetry.
    """
    if speed_knots is None and alt_feet is None:
        return FlightPhase.UNKNOWN

    speed = speed_knots or 0.0
    alt = alt_feet or 0.0

    # Calculate vertical rate in feet per minute (fpm) if history is available
    vs_fpm = 0.0
    if prev_alt_feet is not None and dt_seconds is not None and dt_seconds > 0:
        vs_fpm = ((alt_feet - prev_alt_feet) / dt_seconds) * 60.0

    # Low speed on ground or near surface
    if speed < 35.0 and alt < 1000.0:
        return FlightPhase.TAXIING if speed > 5.0 else FlightPhase.LANDED

    # High speed near ground climbing rapidly -> Takeoff
    if alt < 3000.0 and vs_fpm > 500.0:
        return FlightPhase.TAKEOFF

    # Low altitude & descending -> Approach
    if alt < 4000.0 and vs_fpm < -300.0:
        return FlightPhase.APPROACH

    # Sustained climb
    if vs_fpm > 300.0:
        return FlightPhase.CLIMB

    # Sustained descent
    if vs_fpm < -300.0:
        return FlightPhase.DESCENT

    # Steady altitude above 10,000 ft or cruising speed
    if alt >= 10000.0 or (speed > 150.0 and abs(vs_fpm) <= 300.0):
        return FlightPhase.CRUISE

    return FlightPhase.UNKNOWN
