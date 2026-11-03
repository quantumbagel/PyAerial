"""
Geospatial math: heading, speed, distance, and geofence entry-time estimates.

Geofences are passed in as prebuilt :class:`shapely.Polygon` objects so they can
be computed once (see :func:`build_polygons`) rather than rebuilt every tick.
"""

from __future__ import annotations

import math

import shapely
from geopy.distance import geodesic
from shapely import LineString, Point, Polygon
from shapely.ops import nearest_points
from shapely.validation import explain_validity

from pyaerial.constants import (
    MAX_SPEED_MPS,
    METERS_PER_DEG_LAT,
    MIN_METERS_PER_DEG_LON,
    MIN_SPEED_DT,
)
from pyaerial.units import KMH_TO_MPS, MPS_TO_KMH

# One shared instance: geopy rebuilds the ellipsoid model on every
# ``geodesic(...)`` call, which was ~40% of each distance/destination call.
# ``measure`` returns km; ``destination`` takes km. Stateless after setup.
_GEODESIC = geodesic()


def meters_per_deg_lon(lat: float) -> float:
    return max(METERS_PER_DEG_LAT * math.cos(math.radians(lat)), MIN_METERS_PER_DEG_LON)


def wrap_longitude(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0


def shortest_lon_delta(lon: float, lon0: float) -> float:
    """Signed longitude difference in (-180, 180]."""
    return ((lon - lon0 + 180.0) % 360.0) - 180.0


def build_polygons(zones: dict) -> dict[str, Polygon]:
    """Build a ``{zone_name: Polygon}`` map from the configured zones."""
    polygons: dict[str, Polygon] = {}
    for name, zone in zones.items():
        coordinates = zone.coordinates
        if not coordinates:
            raise ValueError(f"zone {name!r} has no coordinates")
        poly = Polygon(coordinates)
        # A bowtie or zero-area ring makes every intersects/covers test
        # meaningless (shapely answers, but not about the drawn zone).
        if not poly.is_valid or poly.area <= 0:
            reason = explain_validity(poly)
            if poly.is_valid:
                reason = "zero area"
            raise ValueError(f"zone {name!r}: {reason}")
        polygons[name] = poly
    return polygons


def _local_frame(polygon: Polygon, lon0: float) -> Polygon:
    """*polygon* as the copy nearest longitude *lon0*, never wrapping the globe.

    Zone math is planar in (lat, lon). Near the antimeridian a zone a few
    km across 180 deg sits ~360 deg away in that plane, and a zone whose
    vertices straddle 180 deg spans the rest of the globe. Each vertex is
    taken the short way from the first (zones are far narrower than 180
    deg), then the whole zone is moved by a multiple of 360 deg so its
    centre is within 180 deg of the aircraft.
    """
    _min_lat, min_lon, _max_lat, max_lon = polygon.bounds
    if max_lon - min_lon < 180.0 and lon0 - 180.0 < min_lon and max_lon <= lon0 + 180.0:
        return polygon

    def shift(coords):  # (N, 2) array of (lat, lon)
        out = coords.copy()
        ref = coords[0, 1]
        lons = ref + ((coords[:, 1] - ref + 180.0) % 360.0) - 180.0
        centre = (lons.min() + lons.max()) / 2.0
        out[:, 1] = lons + 360.0 * round((lon0 - centre) / 360.0)
        return out

    return shapely.transform(polygon, shift)


def calculate_heading(
    previous: tuple[float, float], current: tuple[float, float]
) -> float:
    """Great-circle initial bearing (degrees from true north) between two points."""
    pi_c = math.pi / 180
    first_lat, first_lon = previous[0] * pi_c, previous[1] * pi_c
    second_lat, second_lon = current[0] * pi_c, current[1] * pi_c

    y = math.sin(second_lon - first_lon) * math.cos(second_lat)
    x = (math.cos(first_lat) * math.sin(second_lat)) - (
        math.sin(first_lat) * math.cos(second_lat) * math.cos(second_lon - first_lon)
    )
    return ((math.atan2(y, x) * 180 / math.pi) + 360) % 360


def calculate_speed(
    previous: tuple[float, float],
    current: tuple[float, float],
    previous_time: float,
    current_time: float,
) -> float | None:
    """Average ground speed (km/h) implied by moving between two fixes.

    Returns ``None`` when the interval is too short to estimate speed so the
    caller can keep the previous value instead of treating the sample as a stop.
    """
    elapsed = current_time - previous_time
    if elapsed < MIN_SPEED_DT:
        return None
    speed_mps = _short_distance_m(previous, current) / elapsed
    return min(speed_mps, MAX_SPEED_MPS) * MPS_TO_KMH


# WGS84, for the local-ellipsoid short-distance formula.
_WGS84_A = 6_378_137.0
_WGS84_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
# Below this separation (deg) the local formula is within ~1e-5 of the
# geodesic (checked against geographiclib up to 20 km, |lat| <= 80).
_SHORT_DISTANCE_DEG = 0.15


def _short_distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Ellipsoidal distance (m), fast for the short hops between fixes.

    Uses the meridian and prime-vertical radii at the mid-latitude; falls
    back to the full geodesic for longer or near-polar separations.
    """
    dlat_deg = b[0] - a[0]
    dlon_deg = shortest_lon_delta(b[1], a[1])
    mid = (a[0] + b[0]) / 2.0
    if (
        abs(dlat_deg) > _SHORT_DISTANCE_DEG
        or abs(dlon_deg) > _SHORT_DISTANCE_DEG
        or abs(mid) > 80.0
    ):
        return _GEODESIC.measure(a, b) * 1000.0
    lat = math.radians(mid)
    sin_lat = math.sin(lat)
    w = math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    meridian = _WGS84_A * (1.0 - _WGS84_E2) / (w * w * w)
    prime = _WGS84_A / w
    return math.hypot(
        math.radians(dlat_deg) * meridian,
        math.radians(dlon_deg) * prime * math.cos(lat),
    )


def distance_to_polygon(polygon: Polygon, position: tuple[float, float]) -> float:
    """Shortest distance (km) from ``position`` to the edge of ``polygon``."""
    polygon = _local_frame(polygon, position[1])
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0
    nearest = nearest_points(polygon, point)[0]
    return _GEODESIC.measure((nearest.x, nearest.y), position)


def _bbox_min_distance_km(polygon: Polygon, position: tuple[float, float]) -> float:
    """Lower-bound km from ``position`` to ``polygon`` using the bounding box."""
    min_lat, min_lon, max_lat, max_lon = polygon.bounds
    lat_diff = max(0.0, min_lat - position[0], position[0] - max_lat)
    max_abs_lat = max(abs(position[0]), abs(min_lat), abs(max_lat))
    if max_abs_lat < 89.0:
        lon_diff = max(0.0, min_lon - position[1], position[1] - max_lon)
        return max(
            lat_diff * 110.5, lon_diff * 111.3 * math.cos(math.radians(max_abs_lat))
        )
    return lat_diff * 110.5


def time_to_enter_geofence(
    position: tuple[float, float],
    heading: float,
    speed: float,
    polygon: Polygon,
    max_time: int,
) -> float:
    """
    Estimate the seconds until a plane at ``position`` (heading/speed) enters the
    geofence, or ``math.inf`` if the projected path never intersects it.
    """
    polygon = _local_frame(polygon, position[1])
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0

    if speed <= 0:
        return math.inf

    distance_approx = max_time * speed / 3600  # km
    if _bbox_min_distance_km(polygon, position) > distance_approx:
        return math.inf

    # 2. Continuous ray calculation without discretized steps
    dest = _GEODESIC.destination(position, heading, distance=distance_approx)
    dest_lon = position[1] + shortest_lon_delta(dest.longitude, position[1])
    ray = LineString([point, Point(dest.latitude, dest_lon)])

    # 3. Robust entry calculation using nearest_points to support multi-part geometries
    intersection = ray.intersection(polygon)
    if intersection.is_empty:
        return math.inf

    _, entry_point = nearest_points(point, intersection)
    entry = (entry_point.x, entry_point.y)
    return _GEODESIC.measure(position, entry) / speed * 3600


def time_to_enter_geofence_curved(
    position: tuple[float, float],
    heading: float,
    speed: float,
    turn_rate: float,
    polygon: Polygon,
    max_time: int,
) -> float:
    """
    Estimate seconds until a plane enters the geofence along a curved trajectory
    that accounts for *turn_rate* (degrees/second).

    Constant speed with a constant turn rate produces a perfect circular path, so
    the entry time is solved analytically by intersecting the turn circle with
    the geofence edges without time stepping or discretization error. Falls
    back to :func:`time_to_enter_geofence` when the turn rate is negligible.

    Returns the *earliest* plausible entry: when the turn circle misses the
    geofence (small turn rates imply huge circles), the straight-line estimate is
    reported instead so transient heading noise or the start of a maneuver never
    erases alert forewarning.
    """
    polygon = _local_frame(polygon, position[1])
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0

    if speed <= 0:
        return math.inf

    # Fall back to straight-line when not turning
    if abs(turn_rate) < 0.1:
        return time_to_enter_geofence(position, heading, speed, polygon, max_time)

    # Bounding-box lower-bound check: within max_time the aircraft can cover at
    # most max_time * speed, so if even the straight-line distance to the box
    # exceeds that, the geofence is unreachable.
    if _bbox_min_distance_km(polygon, position) > max_time * speed / 3600:
        return math.inf

    # The straight-line ETA acts as a floor: a small turn rate (heading noise or
    # the start of a maneuver) implies a huge circle that may miss a small
    # geofence even though the aircraft could reach it by holding course, so
    # report whichever model predicts the earliest entry.
    straight_eta = time_to_enter_geofence(position, heading, speed, polygon, max_time)

    # --- Analytic turn-circle intersection (flat-earth, accurate <100 km) ---
    m_per_deg_lat = METERS_PER_DEG_LAT
    m_per_deg_lon = meters_per_deg_lon(position[0])
    lat0, lon0 = position

    omega = math.radians(turn_rate)  # signed rad/s; positive = right turn
    v_ms = speed * KMH_TO_MPS
    radius = v_ms / abs(omega)
    period = 2.0 * math.pi / abs(omega)
    hdg = math.radians(heading)

    # Turn-circle center in local planar (east, north) meters, plus the aircraft's
    # starting angle on the circle: the center sits to the right of the velocity
    # for a right turn and to the left for a left turn.
    if omega > 0:
        cx, cy = radius * math.cos(hdg), -radius * math.sin(hdg)
        alpha0 = math.pi - hdg
    else:
        cx, cy = -radius * math.cos(hdg), radius * math.sin(hdg)
        alpha0 = -hdg

    def position_at(t: float) -> tuple[float, float]:
        """Aircraft position after t seconds along the turn circle."""
        alpha = alpha0 - omega * t
        east = cx + radius * math.cos(alpha)
        north = cy + radius * math.sin(alpha)
        return lat0 + north / m_per_deg_lat, lon0 + east / m_per_deg_lon

    def inside_at(t: float) -> bool:
        return polygon.intersects(Point(position_at(t)))

    # Collect every time within one revolution at which the circle crosses a
    # geofence edge, by intersecting the circle with each edge segment.
    geoms = polygon.geoms if hasattr(polygon, "geoms") else [polygon]
    rings: list[list[tuple[float, float]]] = []
    for geom in geoms:
        rings.append(list(geom.exterior.coords))
        rings.extend(list(interior.coords) for interior in geom.interiors)

    candidates: list[float] = []
    for ring in rings:
        local = [
            (shortest_lon_delta(lon, lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat)
            for lat, lon in ring
        ]
        for (ax, ay), (bx, by) in zip(local, local[1:], strict=False):
            dx, dy = bx - ax, by - ay
            fx, fy = ax - cx, ay - cy
            aa = dx * dx + dy * dy
            if aa <= 0:
                continue
            bb = 2.0 * (fx * dx + fy * dy)
            cc = fx * fx + fy * fy - radius * radius
            disc = bb * bb - 4.0 * aa * cc
            if disc < 0:
                continue
            sq = math.sqrt(disc)
            for root in ((-bb - sq) / (2.0 * aa), (-bb + sq) / (2.0 * aa)):
                if root < 0.0 or root > 1.0:
                    continue
                pe = ax + root * dx
                pn = ay + root * dy
                alpha = math.atan2(pn - cy, pe - cx)
                t = (alpha0 - alpha) / omega
                t %= period
                if t <= 1e-6:
                    t += period
                if t <= max_time:
                    candidates.append(t)

    candidates.sort()
    # The aircraft starts outside the geofence, so the first crossing after which
    # it is inside is the entry time.
    probe_delta = 1e-6
    for t in candidates:
        if inside_at(t + probe_delta):
            return min(t, straight_eta)
    return straight_eta


def dead_reckon_curved(
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
    turn_rate: float,
    dt: float,
    sub_steps: int = 10,
) -> tuple[float, float]:
    """
    Project *position* forward by *dt* seconds along a curved trajectory
    defined by *heading*, *speed_kph*, and *turn_rate* (deg/s).

    Returns ``(lat, lon)`` of the predicted future position.
    """
    if dt <= 0 or speed_kph <= 0:
        return position

    step_dt = dt / sub_steps
    lat, lon = position
    cur_heading = heading
    speed_m_s = speed_kph * KMH_TO_MPS
    m_per_deg_lat = METERS_PER_DEG_LAT

    for _ in range(sub_steps):
        cur_heading += turn_rate * step_dt
        dist_m = speed_m_s * step_dt
        rad_h = math.radians(cur_heading)
        m_per_deg_lon = meters_per_deg_lon(lat)
        lat += (dist_m * math.cos(rad_h)) / m_per_deg_lat
        lon += (dist_m * math.sin(rad_h)) / m_per_deg_lon

    return lat, wrap_longitude(lon)
