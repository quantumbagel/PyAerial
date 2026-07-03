"""
Requirement / component evaluation shared by live alerting and persistence.

A "component" is a set of numeric constraints on data fields; a "requirement" is
a boolean expression over component names. Both the live alert path
(:mod:`pyaerial.calc.plane`) and the save-decision path
(:class:`pyaerial.savers.Saver`) resolve field values differently (latest value
vs. value at a historical timestamp), so this module accepts a ``resolver``
callable that maps a data-field name to its value (or ``None`` if unavailable).
"""
from __future__ import annotations

from typing import Callable

from shapely import Polygon

from pyaerial import expr
from pyaerial.calc import geo
from pyaerial.config.schema import ComparisonSpec
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


def make_resolver(plane: dict, eta: float, polygon: Polygon,
                  position: tuple[float, float], at_time: float | None = None) -> Resolver:
    """Build a resolver that reads a plane's data fields (optionally at a time)."""

    def resolve(field: str) -> float | None:
        if field in _RECV_FIELDS:
            datum = get_latest(STORE_RECV_DATA, field, plane, at_time)
            return datum.value if datum else None
        if field in _CALC_FIELDS:
            datum = get_latest(STORE_CALC_DATA, field, plane, at_time)
            return datum.value if datum else None
        if field == ALERT_CAT_ETA:
            return eta
        if field == STORE_DISTANCE:
            return geo.distance_to_polygon(polygon, position)
        return None

    return resolve


def component_passes(component: dict[str, ComparisonSpec], resolver: Resolver) -> bool:
    """Return whether every constraint in a component is satisfied."""
    for field, spec in component.items():
        value = resolver(field)
        if value is None:
            return False
        for ctype, threshold in spec.as_pairs().items():
            if not CONFIG_COMP_FUNCTIONS[ctype](value, threshold):
                return False
    return True


def requirement_passes(requirement: str, components_cfg: dict, resolver: Resolver) -> bool:
    """Evaluate a requirement expression over the configured components."""
    results = {
        name: component_passes(components_cfg[name], resolver)
        for name in expr.extract_component_names(requirement)
    }
    return expr.evaluate(requirement, results)
