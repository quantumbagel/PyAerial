"""
Requirement / field-constraint evaluation shared by live alerting and persistence.
"""
from __future__ import annotations

from typing import Callable

from shapely import Polygon

from pyaerial.calc import geo
from pyaerial.config.schema import FieldConstraint
from pyaerial.constants import (
    ALERT_CAT_ETA,
    CONFIG_COMP_FUNCTIONS,
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_DISTANCE,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
    STORE_VERT_SPEED,
)
from pyaerial.models import get_latest

Resolver = Callable[[str], float | None]

_RECV_FIELDS = {STORE_LAT, STORE_LONG, STORE_ALT, STORE_VERT_SPEED}
_CALC_FIELDS = {STORE_HORIZ_SPEED, STORE_HEADING}
_FIELD_ALIASES = {
    "speed": STORE_HORIZ_SPEED,
    "horizontal_speed": STORE_HORIZ_SPEED,
    "heading": STORE_HEADING,
    "direction": STORE_HEADING,
    "alt": STORE_ALT,
    "altitude": STORE_ALT,
    "vert_speed": STORE_VERT_SPEED,
    "vertical_speed": STORE_VERT_SPEED,
    "lat": STORE_LAT,
    "latitude": STORE_LAT,
    "lon": STORE_LONG,
    "long": STORE_LONG,
    "longitude": STORE_LONG,
    "dist": STORE_DISTANCE,
    "distance": STORE_DISTANCE,
    "eta": ALERT_CAT_ETA,
}


def make_resolver(plane: dict, eta: float, polygon: Polygon,
                  position: tuple[float, float], at_time: float | None = None) -> Resolver:
    """Build a resolver that reads a plane's data fields (optionally at a time)."""

    def resolve(field: str) -> float | None:
        target_field = _FIELD_ALIASES.get(field, field)
        if target_field in _RECV_FIELDS:
            datum = get_latest(STORE_RECV_DATA, target_field, plane, at_time)
            return datum.value if datum else None
        if target_field in _CALC_FIELDS:
            datum = get_latest(STORE_CALC_DATA, target_field, plane, at_time)
            return datum.value if datum else None
        if target_field == ALERT_CAT_ETA:
            return eta
        if target_field == STORE_DISTANCE:
            return geo.distance_to_polygon(polygon, position)
        return None

    return resolve


def when_passes(when: dict[str, FieldConstraint], resolver: Resolver) -> bool:
    """Return whether every field constraint in a rule's ``when`` block is satisfied."""
    for field, spec in when.items():
        value = resolver(field)
        if value is None:
            return False
        for ctype, threshold in spec.as_pairs().items():
            if not CONFIG_COMP_FUNCTIONS[ctype](value, threshold):
                return False
    return True
