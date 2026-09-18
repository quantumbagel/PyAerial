from __future__ import annotations

import math

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
    assert speed == 0.0


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
