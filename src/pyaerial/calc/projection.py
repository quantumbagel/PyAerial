"""Map projection polylines (turn-rate curved forward extrapolation)."""
from __future__ import annotations

from typing import Any

from pyaerial.calc import geo
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import Config


def _sample_track_path(
    position: tuple[float, float],
    motion: ResolvedMotion,
    *,
    horizon_seconds: float,
    step_seconds: float,
) -> list[list[float]]:
    if motion.speed_kph <= 0 or horizon_seconds <= 0 or step_seconds <= 0:
        return [[position[0], position[1]]]

    points: list[list[float]] = [[position[0], position[1]]]
    t = step_seconds
    while t <= horizon_seconds + 1e-9:
        lat, lon = geo.dead_reckon_curved(
            position,
            motion.heading_deg,
            motion.speed_kph,
            motion.turn_rate_deg_s,
            t,
        )
        points.append([lat, lon])
        t += step_seconds
    return points


def build_portal_projection(
    config: Config,
    position: tuple[float, float],
    motion: ResolvedMotion,
) -> dict[str, Any]:
    """Build a forward path for the web map."""
    tracking = config.tracking
    horizon = tracking.projection_seconds
    step = tracking.projection_step_seconds

    track_path = _sample_track_path(
        position,
        motion,
        horizon_seconds=horizon,
        step_seconds=step,
    )

    return {
        "horizon_seconds": horizon,
        "step_seconds": step,
        "track_path": track_path,
        "motion_heading": motion.heading_deg,
        "motion_speed_kph": motion.speed_kph,
        "turn_rate_deg_s": motion.turn_rate_deg_s,
    }
