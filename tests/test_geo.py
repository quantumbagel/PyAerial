from __future__ import annotations

import math
import random

import pytest
from geopy.distance import geodesic
from shapely import Polygon

from pyaerial.calc import geo
from pyaerial.constants import MAX_SPEED_MPS
from pyaerial.units import MPS_TO_KMH


def test_inside_polygon_eta_is_zero():
    polygon = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69), (35.72, -78.69)])
    assert geo.time_to_enter_geofence((35.725, -78.695), 0.0, 200.0, polygon, 1000) == 0.0


def test_receding_track_is_inf():
    # Plane south of the box, heading south (away).
    polygon = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69), (35.72, -78.69)])
    eta = geo.time_to_enter_geofence((35.70, -78.695), 180.0, 200.0, polygon, 120)
    assert eta is math.inf


def test_calculate_speed_is_kmh():
    # ~111 km of latitude in 1 hour → ~111 km/h
    speed = geo.calculate_speed((35.0, -78.0), (36.0, -78.0), 0.0, 3600.0)
    assert 100.0 < speed < 130.0


def test_calculate_speed_rejects_microsecond_dt():
    speed = geo.calculate_speed((35.7, -78.7), (35.7005, -78.7), 1.0, 1.0 + 1e-6)
    assert speed is None


def test_wrap_longitude_and_shortest_delta():
    assert geo.wrap_longitude(181.0) == -179.0
    assert abs(geo.shortest_lon_delta(-179.9, 179.9)) < 0.3
    lat, lon = geo.dead_reckon_curved((0.0, 179.9), 90.0, 400.0, 0.0, 120.0)
    assert lon <= 180.0
    assert lat == 0.0 or abs(lat) < 1.0


def test_calculate_speed_clamps_unphysical():
    # 1 deg latitude in 0.5s is thousands of m/s.
    speed = geo.calculate_speed((35.0, -78.0), (36.0, -78.0), 0.0, 0.5)
    assert speed == MAX_SPEED_MPS * MPS_TO_KMH


def test_predict_seconds_straight_when_curved_disabled():
    from pyaerial.calc import evaluate

    polygon = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69), (35.72, -78.69)])
    plane: dict = {}
    curved = evaluate.make_predicted_resolver(
        plane, polygon, (35.70, -78.695), 0.0, 400.0, 10.0, 30.0, curved=True
    )
    straight = evaluate.make_predicted_resolver(
        plane, polygon, (35.70, -78.695), 0.0, 400.0, 10.0, 30.0, curved=False
    )
    assert curved("eta") != straight("eta") or curved("distance") != straight("distance")


# --- Antimeridian geofences and invalid polygons -----------------------------


def _box(lat0, lat1, lon0, lon1) -> Polygon:
    return Polygon(
        [(lat0, lon0), (lat1, lon0), (lat1, lon1), (lat0, lon1), (lat0, lon0)]
    )


def test_eta_and_distance_to_zone_across_the_antimeridian():
    position = (35.0, 179.8)
    zone = _box(34.9, 35.1, -179.7, -179.5)
    distance = geo.distance_to_polygon(zone, position)
    expected = geodesic(position, (35.0, -179.7)).km
    assert distance == pytest.approx(expected, rel=0.01)
    eta = geo.time_to_enter_geofence(position, 90.0, 900.0, zone, 600)
    assert eta == pytest.approx(expected / 900.0 * 3600, rel=0.02)
    curved = geo.time_to_enter_geofence_curved(position, 90.0, 900.0, 0.5, zone, 600)
    assert math.isfinite(curved)
    assert curved <= eta + 1e-6


def test_zone_straddling_the_antimeridian():
    zone = _box(34.9, 35.1, 179.9, -179.9)
    assert geo.distance_to_polygon(zone, (35.0, 179.95)) == 0.0
    assert geo.distance_to_polygon(zone, (35.0, -179.95)) == 0.0
    assert geo.time_to_enter_geofence((35.0, -179.95), 0.0, 500.0, zone, 600) == 0.0
    # Planar math put the far side of the globe "inside" this zone.
    assert geo.distance_to_polygon(zone, (35.0, 0.0)) > 10_000
    assert geo.time_to_enter_geofence((35.0, 0.0), 90.0, 900.0, zone, 600) == math.inf
    near = geo.distance_to_polygon(zone, (35.0, 179.0))
    assert near == pytest.approx(geodesic((35.0, 179.0), (35.0, 179.9)).km, rel=0.01)


def test_geofence_math_unchanged_away_from_the_antimeridian():
    zone = _box(35.72, 35.73, -78.70, -78.69)
    position = (35.70, -78.695)
    assert geo.distance_to_polygon(zone, position) == pytest.approx(
        geodesic(position, (35.72, -78.695)).km, rel=0.01
    )
    eta = geo.time_to_enter_geofence(position, 0.0, 360.0, zone, 600)
    assert eta == pytest.approx(
        geodesic(position, (35.72, -78.695)).km / 360 * 3600, rel=0.02
    )


# --- Short distances and the shared geodesic ---------------------------------


def test_short_distance_matches_the_geodesic():
    rng = random.Random(5)
    for _ in range(2000):
        lat = rng.uniform(-89.0, 89.0)
        lon = rng.uniform(-180.0, 180.0)
        km = rng.choice([0.01, 0.2, 5.0, 40.0, 300.0])
        p = geodesic(kilometers=km).destination((lat, lon), rng.uniform(0, 360))
        b = (p.latitude, p.longitude)
        expected = geodesic((lat, lon), b).m
        assert geo._short_distance_m((lat, lon), b) == pytest.approx(expected, rel=1e-5)
    # Across the antimeridian.
    assert geo._short_distance_m((35.0, 179.999), (35.0, -179.999)) == pytest.approx(
        geodesic((35.0, 179.999), (35.0, -179.999)).m, rel=1e-6
    )


def test_geofence_helpers_use_kilometres_with_the_shared_geodesic():
    assert geo.calculate_speed(
        (35.0, -78.0), (35.01, -78.0), 0.0, 10.0
    ) == pytest.approx(geodesic((35.0, -78.0), (35.01, -78.0)).m / 10.0 * 3.6, rel=1e-5)
