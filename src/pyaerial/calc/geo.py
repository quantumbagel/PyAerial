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
    return {name: Polygon(zone.coordinates) for name, zone in zones.items()}


def calculate_heading(previous: tuple[float, float], current: tuple[float, float]) -> float:
    """Great-circle initial bearing (degrees from true north) between two points."""
    pi_c = math.pi / 180
    first_lat, first_lon = previous[0] * pi_c, previous[1] * pi_c
    second_lat, second_lon = current[0] * pi_c, current[1] * pi_c

    y = math.sin(second_lon - first_lon) * math.cos(second_lat)
    x = (math.cos(first_lat) * math.sin(second_lat)) - (
        math.sin(first_lat) * math.cos(second_lat) * math.cos(second_lon - first_lon))
    return ((math.atan2(y, x) * 180 / math.pi) + 360) % 360


def calculate_speed(previous: tuple[float, float], current: tuple[float, float],
                    previous_time: float, current_time: float) -> float:
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


def time_to_enter_geofence(position: tuple[float, float], heading: float, speed: float,
                           polygon: Polygon, max_time: int) -> float:
    """
    Estimate the seconds until a plane at ``position`` (heading/speed) enters the
    geofence, or ``math.inf`` if the projected path never intersects it.
    """
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0

    if speed <= 0:
        return math.inf

    # 1. Bounding box lower bound distance check (extremely fast early-return)
    min_lat, min_lon, max_lat, max_lon = polygon.bounds
    lat_diff = max(0.0, min_lat - position[0], position[0] - max_lat)
    max_abs_lat = max(abs(position[0]), abs(min_lat), abs(max_lat))
    if max_abs_lat < 89.0:
        lon_diff = max(0.0, min_lon - position[1], position[1] - max_lon)
        min_dist_lb = max(lat_diff * 110.5, lon_diff * 111.3 * math.cos(math.radians(max_abs_lat)))
    else:
        min_dist_lb = lat_diff * 110.5

    distance_approx = max_time * speed / 3600  # km
    if min_dist_lb > distance_approx:
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
    steps: int = 30,
) -> float:
    """
    Estimate seconds until a plane enters the geofence using a curved trajectory
    that accounts for *turn_rate* (degrees/second).

    Falls back to :func:`time_to_enter_geofence` when turn rate is negligible.
    Uses flat-earth approximation for the stepping loop (accurate for <100 km).
    """
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0

    if speed <= 0:
        return math.inf

    # Fall back to straight-line when not turning
    if abs(turn_rate) < 0.1:
        return time_to_enter_geofence(position, heading, speed, polygon, max_time)

    # Bounding-box early return (same logic as straight-line version)
    min_lat, min_lon, max_lat, max_lon = polygon.bounds
    lat_diff = max(0.0, min_lat - position[0], position[0] - max_lat)
    max_abs_lat = max(abs(position[0]), abs(min_lat), abs(max_lat))
    if max_abs_lat < 89.0:
        lon_diff = max(0.0, min_lon - position[1], position[1] - max_lon)
        min_dist_lb = max(lat_diff * 110.5,
                          lon_diff * 111.3 * math.cos(math.radians(max_abs_lat)))
    else:
        min_dist_lb = lat_diff * 110.5

    distance_approx = max_time * speed / 3600  # km
    if min_dist_lb > distance_approx:
        return math.inf

    # Step forward along curved trajectory
    dt = max_time / steps
    lat, lon = position
    cur_heading = heading
    speed_m_s = speed / 3.6
    m_per_deg_lat = 111_000.0

    prev_lat, prev_lon = lat, lon
    for i in range(1, steps + 1):
        cur_heading += turn_rate * dt
        dist_m = speed_m_s * dt
        rad_h = math.radians(cur_heading)
        m_per_deg_lon = max(111_000.0 * math.cos(math.radians(lat)), 1000.0)
        lat += (dist_m * math.cos(rad_h)) / m_per_deg_lat
        lon += (dist_m * math.sin(rad_h)) / m_per_deg_lon

        segment = LineString([Point(prev_lat, prev_lon), Point(lat, lon)])
        if segment.intersects(polygon):
            # Binary-search for a more precise entry time
            lo_t = dt * (i - 1)
            hi_t = dt * i
            for _ in range(8):
                mid_t = (lo_t + hi_t) / 2
                mid_pos = dead_reckon_curved(position, heading, speed, turn_rate, mid_t)
                if polygon.intersects(Point(mid_pos)):
                    hi_t = mid_t
                else:
                    lo_t = mid_t
            return (lo_t + hi_t) / 2

        prev_lat, prev_lon = lat, lon

    return math.inf


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


def time_to_enter_geofence_intent(
    position: tuple[float, float],
    heading: float,
    speed: float,
    turn_rate: float,
    selected_heading: float,
    polygon: Polygon,
    max_time: int,
) -> float:
    """
    Estimate ETA using autopilot intent data from ADS-B TC 29.

    Simulates the aircraft turning at *turn_rate* (or standard 3 deg/s if not
    yet turning) until it reaches *selected_heading*, then projects straight
    along that heading.  Returns the earliest time the projected path enters
    the polygon, or ``math.inf`` if no entry is predicted.
    """
    point = Point(position)
    if polygon.intersects(point) or polygon.covers(point):
        return 0.0

    if speed <= 0:
        return math.inf

    # Shortest angular distance from current heading to selected heading
    delta = (selected_heading - heading + 540.0) % 360.0 - 180.0

    # If headings are already aligned, just project straight
    if abs(delta) < 1.0:
        return time_to_enter_geofence(position, selected_heading, speed, polygon, max_time)

    # Use observed turn rate if significant, otherwise assume standard rate
    if abs(turn_rate) >= 0.5:
        effective_rate = turn_rate
    else:
        effective_rate = 3.0 if delta > 0 else -3.0

    # Make sure the effective rate turns us in the right direction
    if (delta > 0 and effective_rate < 0) or (delta < 0 and effective_rate > 0):
        effective_rate = -effective_rate

    turn_time = abs(delta / effective_rate)

    # --- Phase 1: curved path during the turn ---
    steps = max(int(turn_time * 2) + 1, 10)
    dt = turn_time / steps
    lat, lon = position
    cur_heading = heading
    speed_m_s = speed / 3.6
    m_per_deg_lat = 111_000.0

    prev_lat, prev_lon = lat, lon
    for i in range(1, steps + 1):
        cur_heading += effective_rate * dt
        dist_m = speed_m_s * dt
        rad_h = math.radians(cur_heading)
        m_per_deg_lon = max(111_000.0 * math.cos(math.radians(lat)), 1000.0)
        lat += (dist_m * math.cos(rad_h)) / m_per_deg_lat
        lon += (dist_m * math.sin(rad_h)) / m_per_deg_lon

        segment = LineString([Point(prev_lat, prev_lon), Point(lat, lon)])
        if segment.intersects(polygon):
            lo_t = dt * (i - 1)
            hi_t = dt * i
            for _ in range(8):
                mid_t = (lo_t + hi_t) / 2
                mid_pos = dead_reckon_curved(
                    position, heading, speed, effective_rate, mid_t,
                )
                if polygon.intersects(Point(mid_pos)):
                    hi_t = mid_t
                else:
                    lo_t = mid_t
            return (lo_t + hi_t) / 2

        prev_lat, prev_lon = lat, lon

    # --- Phase 2: straight-line from end of turn at selected heading ---
    remaining_time = max_time - turn_time
    if remaining_time <= 0:
        return math.inf

    turn_end = (lat, lon)
    straight_eta = time_to_enter_geofence(
        turn_end, selected_heading, speed, polygon, int(remaining_time),
    )
    if straight_eta < math.inf:
        return turn_time + straight_eta

    return math.inf
