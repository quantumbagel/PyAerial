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



