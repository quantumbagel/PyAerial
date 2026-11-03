"""
Requirement / field-constraint evaluation shared by live alerting and persistence.
"""

from __future__ import annotations

from collections.abc import Callable

from shapely import Polygon

from pyaerial.calc import geo
from pyaerial.config.schema import FieldConstraint
from pyaerial.constants import (
    ALERT_CAT_ETA,
    CONFIG_COMP_FUNCTIONS,
    STORE_ALT,
    STORE_DISTANCE,
    STORE_GNSS_HEIGHT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
    STORE_VERT_SPEED,
)
from pyaerial.models import Plane, latest_datum, sample_provenance

Resolver = Callable[[str], float | None]

_RECV_FIELDS = {STORE_LAT, STORE_LONG, STORE_ALT, STORE_VERT_SPEED}
_CALC_FIELDS = {STORE_HORIZ_SPEED, STORE_HEADING}
_PROXIMITY_KEY = "_proximity"
_INTEGRITY_KEY = "integrity"
_HEADING_FIELDS = {STORE_HEADING, "heading", "direction"}
# integrity.monitor.STATUS_UNKNOWN; not imported to keep calc free of the
# integrity package.
_INTEGRITY_UNKNOWN = "unknown"

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
    "proximity": _PROXIMITY_KEY,
    "eta": ALERT_CAT_ETA,
    "integrity": _INTEGRITY_KEY,
}


def canonical_field(field: str) -> str:
    """The internal name a ``when`` field (or alias) resolves to."""
    return _FIELD_ALIASES.get(field, field)


ALTITUDE_BARO = "baro"
ALTITUDE_GNSS = "gnss"


def altitude_m(
    plane: Plane, at_time: float | None = None
) -> tuple[float | None, str | None]:
    """``(metres, source)`` for ``altitude`` rules and alert payloads.

    Barometric altitude wins whenever the aircraft reports it. Only aircraft
    that send GNSS positions alone (TC 20-22) fall back to ``gnss_height``,
    which is *geometric* height above the WGS84 ellipsoid, not pressure
    altitude: the two can differ by tens to hundreds of metres.
    """
    baro = latest_datum(plane.received_data, STORE_ALT, at_time)
    if baro is not None:
        return baro.value, ALTITUDE_BARO
    gnss = latest_datum(plane.received_data, STORE_GNSS_HEIGHT, at_time)
    if gnss is not None:
        return gnss.value, ALTITUDE_GNSS
    return None, None


def integrity_score(plane: Plane, at_time: float | None = None) -> float | None:
    """Integrity score backed by evidence, or None.

    An ``unknown`` (or missing) status publishes score 1.0 without any check
    having run; returning None makes an ``integrity`` constraint fail, like any
    other missing field. With *at_time*, the verdict in effect then is used
    (from the per-plane verdict log), not the latest one.
    """
    if at_time is not None:
        verdict = sample_provenance(plane, at_time)
        score = verdict.get("integrity_score")
        status = verdict.get("integrity_status")
    else:
        doc = plane.info.integrity
        if doc is None:
            return None
        score = doc.get("score")
        status = doc.get("status")
    if not isinstance(status, str) or not status or status == _INTEGRITY_UNKNOWN:
        return None
    return float(score) if isinstance(score, (int, float)) else None


def _heading_in_range(
    value: float, minimum: float | None, maximum: float | None
) -> bool:
    """True if ``value`` (degrees) lies in the (possibly wrapping) heading window."""
    heading = value % 360.0
    if minimum is not None and maximum is not None:
        # {min: 0, max: 360} is "all headings"; 360 % 360 == 0 would collapse it.
        if (maximum - minimum) >= 360.0:
            return True
        lo = minimum % 360.0
        hi = maximum % 360.0
        if lo <= hi:
            return lo <= heading <= hi
        return heading >= lo or heading <= hi
    if minimum is not None:
        return heading >= (minimum % 360.0)
    if maximum is not None:
        if maximum >= 360.0:
            return True
        return heading <= (maximum % 360.0)
    return True


def make_resolver(
    plane: Plane,
    eta: float,
    polygon: Polygon,
    position: tuple[float, float],
    at_time: float | None = None,
) -> Resolver:
    """Build a resolver that reads a plane's data fields (optionally at a time)."""

    def resolve(field: str) -> float | None:
        target_field = _FIELD_ALIASES.get(field, field)
        # lat/lon describe the same (possibly dead-reckoned) position that
        # distance and eta are measured from.
        if target_field == STORE_LAT:
            return position[0]
        if target_field == STORE_LONG:
            return position[1]
        if target_field == STORE_ALT:
            return altitude_m(plane, at_time)[0]
        if target_field in _RECV_FIELDS:
            datum = latest_datum(plane.received_data, target_field, at_time)
            return datum.value if datum else None
        if target_field in _CALC_FIELDS:
            datum = latest_datum(plane.calculated_data, target_field, at_time)
            return datum.value if datum else None
        if target_field == ALERT_CAT_ETA:
            return eta
        if target_field == STORE_DISTANCE:
            return geo.distance_to_polygon(polygon, position)
        if target_field == _PROXIMITY_KEY:
            return geo.distance_to_polygon(polygon, position) * 1000  # km -> m
        if target_field == _INTEGRITY_KEY:
            return integrity_score(plane, at_time)
        return None

    return resolve


def when_passes(when: dict[str, FieldConstraint], resolver: Resolver) -> bool:
    """Return whether every field constraint in a rule's ``when`` block is satisfied."""
    for field, spec in when.items():
        value = resolver(field)
        if value is None:
            return False
        if field in _HEADING_FIELDS or _FIELD_ALIASES.get(field) == STORE_HEADING:
            if not _heading_in_range(value, spec.minimum, spec.maximum):
                return False
            continue
        for ctype, threshold in spec.as_pairs().items():
            if not CONFIG_COMP_FUNCTIONS[ctype](value, threshold):
                return False
    return True


def make_predicted_resolver(
    plane: Plane,
    polygon: Polygon,
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
    turn_rate: float,
    predict_seconds: float,
    curved: bool = False,
) -> Resolver:
    """
    Build a resolver that evaluates against *predicted* future state.

    Projects position forward by ``predict_seconds`` using dead reckoning
    (curved if *curved* is ``True``), extrapolates altitude using vertical
    speed, and recomputes ETA from the predicted position.
    """
    project_turn = turn_rate if curved else 0.0
    predicted_pos = geo.dead_reckon_curved(
        position,
        heading,
        speed_kph,
        project_turn,
        predict_seconds,
    )
    predicted_heading = heading + project_turn * predict_seconds

    if curved and abs(turn_rate) >= 0.1:
        predicted_eta = geo.time_to_enter_geofence_curved(
            predicted_pos,
            predicted_heading,
            speed_kph,
            turn_rate,
            polygon,
            10_000,
        )
    else:
        predicted_eta = geo.time_to_enter_geofence(
            predicted_pos,
            predicted_heading,
            speed_kph,
            polygon,
            10_000,
        )

    def resolve(field: str) -> float | None:
        target_field = _FIELD_ALIASES.get(field, field)

        if target_field == STORE_ALT:
            # Baro, else geometric GNSS height (see altitude_m).
            alt = altitude_m(plane)[0]
            if alt is None:
                return None
            vs = latest_datum(plane.received_data, STORE_VERT_SPEED)
            if vs is not None:
                return alt + vs.value * predict_seconds
            return alt

        if target_field == ALERT_CAT_ETA:
            return max(0.0, predicted_eta)

        if target_field == STORE_LAT:
            return predicted_pos[0]
        if target_field == STORE_LONG:
            return predicted_pos[1]

        if target_field == STORE_DISTANCE:
            return geo.distance_to_polygon(polygon, predicted_pos)

        if target_field == _PROXIMITY_KEY:
            return geo.distance_to_polygon(polygon, predicted_pos) * 1000

        if target_field == _INTEGRITY_KEY:
            return integrity_score(plane)

        # For speed, heading, vert_speed, etc. use current values
        if target_field in _RECV_FIELDS:
            datum = latest_datum(plane.received_data, target_field)
            return datum.value if datum else None
        if target_field in _CALC_FIELDS:
            datum = latest_datum(plane.calculated_data, target_field)
            return datum.value if datum else None

        return None

    return resolve
