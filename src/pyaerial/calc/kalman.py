"""
Kinematic Extended Kalman Filter (EKF) and Dead Reckoning for PyAerial.

Provides 2D state tracking (latitude, longitude, north velocity, east velocity)
for smoothing noisy position reports and dead-reckoning during signal gaps.
"""
from __future__ import annotations

import math
import time

# Approximate meters per degree latitude at mid-latitudes
METERS_PER_DEG_LAT = 111_000.0


def _meters_per_deg_lon(lat: float) -> float:
    return 111_000.0 * math.cos(math.radians(lat))


class KinematicKalmanFilter:
    """
    2D Constant-Velocity Kalman Filter for aircraft tracking.
    State vector: [lat (deg), lon (deg), vn (m/s), ve (m/s)]
    """

    def __init__(self, init_lat: float, init_lon: float,
                 init_vn: float = 0.0, init_ve: float = 0.0,
                 process_noise: float = 1.0, measurement_noise: float = 25.0):
        # State vector
        self.lat = init_lat
        self.lon = init_lon
        self.vn = init_vn  # North velocity (m/s)
        self.ve = init_ve  # East velocity (m/s)

        # Covariance matrix diagonal terms
        self.p_lat = 0.0001
        self.p_lon = 0.0001
        self.p_vn = 100.0
        self.p_ve = 100.0

        self.q = process_noise  # Process noise spectral density
        self.r_pos = measurement_noise  # Measurement error covariance (meters^2)
        self.last_update_time: float = time.time()

    def predict(self, dt: float) -> tuple[float, float]:
        """Predict state forward by dt seconds."""
        if dt <= 0:
            return self.lat, self.lon

        m_per_deg_lat = METERS_PER_DEG_LAT
        m_per_deg_lon = max(_meters_per_deg_lon(self.lat), 1000.0)

        # Position extrapolation in degrees
        self.lat += (self.vn * dt) / m_per_deg_lat
        self.lon += (self.ve * dt) / m_per_deg_lon

        # Covariance growth
        self.p_lat += (dt ** 2 * self.q) / (m_per_deg_lat ** 2)
        self.p_lon += (dt ** 2 * self.q) / (m_per_deg_lon ** 2)
        self.p_vn += dt * self.q
        self.p_ve += dt * self.q

        return self.lat, self.lon

    def update(self, measured_lat: float, measured_lon: float, dt: float) -> tuple[float, float, float, float]:
        """
        Incorporate position measurement and update state.
        Returns (filtered_lat, filtered_lon, speed_m_s, heading_deg).
        """
        if dt > 0:
            self.predict(dt)

        m_per_deg_lat = METERS_PER_DEG_LAT
        m_per_deg_lon = max(_meters_per_deg_lon(measured_lat), 1000.0)

        # Innovation (residual in degrees converted to meters)
        res_lat_m = (measured_lat - self.lat) * m_per_deg_lat
        res_lon_m = (measured_lon - self.lon) * m_per_deg_lon

        # Kalman gain for position (simple decoupled scalar update for numerical efficiency)
        k_lat = (self.p_lat * m_per_deg_lat ** 2) / (self.p_lat * m_per_deg_lat ** 2 + self.r_pos)
        k_lon = (self.p_lon * m_per_deg_lon ** 2) / (self.p_lon * m_per_deg_lon ** 2 + self.r_pos)

        # Update position
        self.lat += (k_lat * res_lat_m) / m_per_deg_lat
        self.lon += (k_lon * res_lon_m) / m_per_deg_lon

        # Update velocity based on position residual if dt > 0
        if dt > 0:
            self.vn += 0.2 * (res_lat_m / dt)
            self.ve += 0.2 * (res_lon_m / dt)

        # Update covariances
        self.p_lat *= (1.0 - k_lat)
        self.p_lon *= (1.0 - k_lon)

        speed_m_s = math.hypot(self.vn, self.ve)
        heading_deg = (math.degrees(math.atan2(self.ve, self.vn)) + 360.0) % 360.0

        return self.lat, self.lon, speed_m_s, heading_deg


def dead_reckon_position(lat: float, lon: float, heading_deg: float,
                         speed_knots: float, dt: float) -> tuple[float, float]:
    """
    Project position forward by dt seconds using heading and speed (dead reckoning).
    """
    if dt <= 0 or speed_knots <= 0:
        return lat, lon

    speed_m_s = speed_knots * 0.514444
    dist_m = speed_m_s * dt

    rad_heading = math.radians(heading_deg)
    delta_north_m = dist_m * math.cos(rad_heading)
    delta_east_m = dist_m * math.sin(rad_heading)

    m_per_deg_lat = METERS_PER_DEG_LAT
    m_per_deg_lon = max(_meters_per_deg_lon(lat), 1000.0)

    new_lat = lat + (delta_north_m / m_per_deg_lat)
    new_lon = lon + (delta_east_m / m_per_deg_lon)

    return new_lat, new_lon
