"""
Decoupled 2D position filter with a heuristic velocity nudge.

Used for optional Kalman-smoothed speed/heading when ``use_kalman_eta`` is on.
Filtered lat/lon are not the live telemetry source.
"""

from __future__ import annotations

import math
import time

from pyaerial.calc.geo import meters_per_deg_lon
from pyaerial.constants import (
    MAX_KALMAN_DT,
    MAX_SPEED_MPS,
    METERS_PER_DEG_LAT,
    MIN_VEL_DT,
)


class KinematicKalmanFilter:
    """
    2D Constant-Velocity Kalman Filter for aircraft tracking.
    State vector: [lat (deg), lon (deg), vn (m/s), ve (m/s)]
    """

    def __init__(
        self,
        init_lat: float,
        init_lon: float,
        init_vn: float = 0.0,
        init_ve: float = 0.0,
        process_noise: float = 1.0,
        measurement_noise: float = 25.0,
    ):
        # State vector
        self.lat = init_lat
        self.lon = init_lon
        self.vn = init_vn  # North velocity (m/s)
        self.ve = init_ve  # East velocity (m/s)

        # Covariance matrix diagonal terms
        self.p_lat = 0.0001
        self.p_lon = 0.0001

        self.q = process_noise  # Process noise spectral density
        self.r_pos = measurement_noise  # Measurement error covariance (meters^2)
        self.last_update_time: float = time.time()

    def predict(self, dt: float) -> tuple[float, float]:
        """Predict state forward by dt seconds."""
        if dt <= 0:
            return self.lat, self.lon

        m_per_deg_lat = METERS_PER_DEG_LAT
        m_per_deg_lon = meters_per_deg_lon(self.lat)

        # Position extrapolation in degrees
        self.lat += (self.vn * dt) / m_per_deg_lat
        self.lon += (self.ve * dt) / m_per_deg_lon

        # Covariance growth
        self.p_lat += (dt**2 * self.q) / (m_per_deg_lat**2)
        self.p_lon += (dt**2 * self.q) / (m_per_deg_lon**2)

        return self.lat, self.lon

    def update(
        self, measured_lat: float, measured_lon: float, dt: float
    ) -> tuple[float, float, float, float]:
        """
        Incorporate position measurement and update state.
        Returns (filtered_lat, filtered_lon, speed_m_s, heading_deg).
        """
        dt = min(max(dt, 0.0), MAX_KALMAN_DT)
        if dt > 0:
            self.predict(dt)

        m_per_deg_lat = METERS_PER_DEG_LAT
        m_per_deg_lon = meters_per_deg_lon(measured_lat)

        # Innovation (residual in degrees converted to meters)
        res_lat_m = (measured_lat - self.lat) * m_per_deg_lat
        res_lon_m = (measured_lon - self.lon) * m_per_deg_lon

        # Kalman gain for position (simple decoupled scalar update for numerical efficiency)
        k_lat = (self.p_lat * m_per_deg_lat**2) / (
            self.p_lat * m_per_deg_lat**2 + self.r_pos
        )
        k_lon = (self.p_lon * m_per_deg_lon**2) / (
            self.p_lon * m_per_deg_lon**2 + self.r_pos
        )

        # Update position
        self.lat += (k_lat * res_lat_m) / m_per_deg_lat
        self.lon += (k_lon * res_lon_m) / m_per_deg_lon

        if dt >= MIN_VEL_DT:
            self.vn += 0.2 * (res_lat_m / dt)
            self.ve += 0.2 * (res_lon_m / dt)

        self.p_lat *= 1.0 - k_lat
        self.p_lon *= 1.0 - k_lon

        speed_m_s = math.hypot(self.vn, self.ve)
        if speed_m_s > MAX_SPEED_MPS:
            scale = MAX_SPEED_MPS / speed_m_s
            self.vn *= scale
            self.ve *= scale
            speed_m_s = MAX_SPEED_MPS
        heading_deg = (math.degrees(math.atan2(self.ve, self.vn)) + 360.0) % 360.0

        return self.lat, self.lon, speed_m_s, heading_deg
