"""WGS84 geometry for antenna-to-aircraft checks (ranges, bearings, horizon)."""

from __future__ import annotations

import math

SPEED_OF_LIGHT = 299_792_458.0
_A = 6_378_137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_MEAN_RADIUS_M = 6_371_008.8
# 4/3-earth radio horizon: d_km = 4.12 * sqrt(h_m).
_HORIZON_K = 4.12

Vec3 = tuple[float, float, float]


def ecef(lat: float, lon: float, alt: float = 0.0) -> Vec3:
    """Geodetic degrees / metres to earth-centred earth-fixed metres."""
    phi = math.radians(lat)
    lam = math.radians(lon)
    sin_phi = math.sin(phi)
    n = _A / math.sqrt(1 - _E2 * sin_phi * sin_phi)
    return (
        (n + alt) * math.cos(phi) * math.cos(lam),
        (n + alt) * math.cos(phi) * math.sin(lam),
        (n * (1 - _E2) + alt) * sin_phi,
    )


def slant_range_m(a: Vec3, b: Vec3) -> float:
    return math.dist(a, b)


def ground_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine, mean earth radius)."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _MEAN_RADIUS_M * math.asin(min(1.0, math.sqrt(h))) / 1000.0


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing from point 1 to point 2, degrees clockwise from true north."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


def elevation_deg(
    lat1: float, lon1: float, alt1: float, lat2: float, lon2: float, alt2: float
) -> float:
    """Elevation angle of point 2 seen from point 1, degrees above the horizon."""
    a = ecef(lat1, lon1, alt1)
    b = ecef(lat2, lon2, alt2)
    d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    rng = math.hypot(*d)
    if rng == 0:
        return 90.0
    phi = math.radians(lat1)
    lam = math.radians(lon1)
    up = (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))
    dot = d[0] * up[0] + d[1] * up[1] + d[2] * up[2]
    return math.degrees(math.asin(max(-1.0, min(1.0, dot / rng))))


def radio_horizon_km(receiver_height_m: float, aircraft_height_m: float) -> float:
    """Line-of-sight limit between two heights over a smooth 4/3 earth."""
    return _HORIZON_K * (
        math.sqrt(max(0.0, receiver_height_m)) + math.sqrt(max(0.0, aircraft_height_m))
    )


def angle_diff_deg(a: float, b: float) -> float:
    """Signed smallest difference ``a - b`` in degrees, in (-180, 180]."""
    diff = (a - b + 180.0) % 360.0 - 180.0
    return 180.0 if diff == -180.0 else diff


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    """Point *distance_m* from (lat, lon) along *bearing* (spherical)."""
    delta = distance_m / _MEAN_RADIUS_M
    theta = math.radians(bearing)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(
        math.sin(p1) * math.cos(delta) + math.cos(p1) * math.sin(delta) * math.cos(theta)
    )
    l2 = l1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(p1),
        math.cos(delta) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def enu_to_geodetic(
    origin: tuple[float, float, float], east: float, north: float, up: float
) -> tuple[float, float, float]:
    """Local east/north/up metres around *origin* back to lat, lon, alt."""
    lat0, lon0, alt0 = origin
    x0, y0, z0 = ecef(lat0, lon0, alt0)
    phi = math.radians(lat0)
    lam = math.radians(lon0)
    sp, cp = math.sin(phi), math.cos(phi)
    sl, cl = math.sin(lam), math.cos(lam)
    x = x0 - sl * east - sp * cl * north + cp * cl * up
    y = y0 + cl * east - sp * sl * north + cp * sl * up
    z = z0 + cp * north + sp * up
    return geodetic(x, y, z)


def geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """ECEF metres to geodetic degrees / metres (Bowring, sub-metre)."""
    b = _A * (1 - _F)
    ep2 = (_A * _A - b * b) / (b * b)
    p = math.hypot(x, y)
    th = math.atan2(z * _A, p * b)
    lat = math.atan2(
        z + ep2 * b * math.sin(th) ** 3, p - _E2 * _A * math.cos(th) ** 3
    )
    lon = math.atan2(y, x)
    n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-12 else abs(z) - b
    return math.degrees(lat), math.degrees(lon), alt
