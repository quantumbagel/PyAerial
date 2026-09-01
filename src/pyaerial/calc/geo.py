"""
Geospatial math: heading, speed, distance, and geofence entry-time estimates.

Geofences are passed in as prebuilt :class:`shapely.Polygon` objects so they can
be computed once (see :func:`build_polygons`) rather than rebuilt every tick.
"""

from __future__ import annotations

import math

from geopy.distance import geodesic
from shapely import LineString, Point, Polygon
from shapely.ops import nearest_points


def build_polygons(zones: dict) -> dict[str, Polygon]:
    """Build a ``{zone_name: Polygon}`` map from the configured zones."""
    polygons: dict[str, Polygon] = {}
    for name, zone in zones.items():
        coordinates = zone.coordinates
        if not coordinates:
            raise ValueError(f"zone {name!r} has no coordinates")
        polygons[name] = Polygon(coordinates)
    return polygons


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
) -> float:
    """Average ground speed (km/h) implied by moving between two fixes."""
    elapsed = current_time - previous_time
    if elapsed <= 0:
        return 0.0
    return geodesic(previous, current).m / elapsed * 3.6


def distance_to_polygon(polygon: Polygon, position: tuple[float, float]) -> float:
    """Shortest distance (km) from ``position`` to the edge of ``polygon``."""
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0
    nearest = nearest_points(polygon, point)[0]
    return geodesic((nearest.x, nearest.y), position).km


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
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0

    if speed <= 0:
        return math.inf

    distance_approx = max_time * speed / 3600  # km
    if _bbox_min_distance_km(polygon, position) > distance_approx:
        return math.inf

    # 2. Continuous ray calculation without discretized steps
    dest = geodesic(kilometers=distance_approx).destination(position, heading)
    ray = LineString([point, Point(dest.latitude, dest.longitude)])

    # 3. Robust entry calculation using nearest_points to support multi-part geometries
    intersection = ray.intersection(polygon)
    if intersection.is_empty:
        return math.inf

    _, entry_point = nearest_points(point, intersection)
    entry = (entry_point.x, entry_point.y)
    return geodesic(position, entry).km / speed * 3600


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
    the geofence edges -- no time stepping, hence no discretization error. Falls
    back to :func:`time_to_enter_geofence` when the turn rate is negligible.

    Returns the *earliest* plausible entry: when the turn circle misses the
    geofence (small turn rates imply huge circles), the straight-line estimate is
    reported instead so transient heading noise or the start of a maneuver never
    erases alert forewarning.
    """
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
    straight_eta = time_to_enter_geofence(
        position, heading, speed, polygon, max_time
    )

    # --- Analytic turn-circle intersection (flat-earth, accurate <100 km) ---
    m_per_deg_lat = 111_000.0
    m_per_deg_lon = max(111_000.0 * math.cos(math.radians(position[0])), 1000.0)
    lat0, lon0 = position

    omega = math.radians(turn_rate)  # signed rad/s; positive = right turn
    v_ms = speed / 3.6
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
            ((lon - lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat)
            for lat, lon in ring
        ]
        for (ax, ay), (bx, by) in zip(local, local[1:]):
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
    speed_m_s = speed_kph / 3.6
    m_per_deg_lat = 111_000.0

    for _ in range(sub_steps):
        cur_heading += turn_rate * step_dt
        dist_m = speed_m_s * step_dt
        rad_h = math.radians(cur_heading)
        m_per_deg_lon = max(111_000.0 * math.cos(math.radians(lat)), 1000.0)
        lat += (dist_m * math.cos(rad_h)) / m_per_deg_lat
        lon += (dist_m * math.sin(rad_h)) / m_per_deg_lon

    return lat, lon
