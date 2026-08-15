from __future__ import annotations

import math

from shapely import Polygon

from pyaerial.calc import geo


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
