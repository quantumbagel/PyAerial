"""Map projection polylines (turn-rate curved forward extrapolation)."""
from __future__ import annotations

import math
from typing import Any

from pyaerial.calc import geo
from pyaerial.calc.kalman import KinematicKalmanFilter
from pyaerial.config.schema import Config


def _sample_track_path(
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
    turn_rate: float,
    *,
    horizon_seconds: float,
    step_seconds: float,
) -> list[list[float]]:
    if speed_kph <= 0 or horizon_seconds <= 0 or step_seconds <= 0:
        return [[position[0], position[1]]]

    points: list[list[float]] = [[position[0], position[1]]]
    t = step_seconds
    while t <= horizon_seconds + 1e-9:
        lat, lon = geo.dead_reckon_curved(position, heading, speed_kph, turn_rate, t)
        points.append([lat, lon])
        t += step_seconds
    return points


def build_portal_projection(
    config: Config,
    kf: KinematicKalmanFilter | None,
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
) -> dict[str, Any]:
    """Build a forward path for the web map (track speed + Kalman heading/turn rate)."""
    tracking = config.tracking
    horizon = tracking.projection_seconds
    step = tracking.projection_step_seconds

    # Path length follows displayed track speed (stable over backdate window).
    motion_speed = max(0.0, speed_kph)
    motion_heading = heading
    turn_rate = kf.turn_rate if kf is not None else 0.0

    if tracking.use_kalman_eta and kf is not None:
        kalman_speed_mps = math.hypot(kf.vn, kf.ve)
        kalman_speed_kph = kalman_speed_mps * 3.6
        if kalman_speed_kph >= 5.0:
            motion_heading = (math.degrees(math.atan2(kf.ve, kf.vn)) + 360.0) % 360.0
        # Keep turn rate from Kalman but cap extreme values from noisy gaps.
        turn_rate = max(-8.0, min(8.0, turn_rate))

    track_path = _sample_track_path(
        position,
        motion_heading,
        motion_speed,
        turn_rate,
        horizon_seconds=horizon,
        step_seconds=step,
    )

    return {
        "horizon_seconds": horizon,
        "step_seconds": step,
        "track_path": track_path,
        "motion_heading": motion_heading,
        "motion_speed_kph": motion_speed,
        "turn_rate_deg_s": turn_rate,
    }
