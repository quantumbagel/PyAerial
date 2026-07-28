"""Shared track motion estimates for map projection and geofence ETA."""
from __future__ import annotations

import math
from dataclasses import dataclass

from pyaerial.calc.kalman import KinematicKalmanFilter
from pyaerial.config.schema import Config


def heading_delta_deg(from_deg: float, to_deg: float) -> float:
    """Signed shortest turn from ``from_deg`` to ``to_deg`` (degrees)."""
    return (to_deg - from_deg + 540.0) % 360.0 - 180.0


def estimate_turn_rate_deg_s(
    heading_now: float,
    heading_then: float,
    elapsed_s: float,
    *,
    prev_smoothed: float | None = None,
    smooth_alpha: float = 0.22,
    straight_threshold: float = 0.28,
    max_rate: float = 3.0,
) -> float:
    """
    Turn rate from heading change over ``elapsed_s``, smoothed over recent updates.

    Small rates are snapped to zero so straight tracks stay straight on the map
    and in curved ETA projection.
    """
    if elapsed_s <= 0:
        return 0.0 if prev_smoothed is None else prev_smoothed

    raw = heading_delta_deg(heading_then, heading_now) / elapsed_s
    if prev_smoothed is not None:
        rate = smooth_alpha * raw + (1.0 - smooth_alpha) * prev_smoothed
    else:
        rate = raw

    if abs(rate) < straight_threshold:
        rate = 0.0
    return max(-max_rate, min(max_rate, rate))


@dataclass(frozen=True)
class ResolvedMotion:
    """Motion used for curved extrapolation (ETA + portal projection)."""

    heading_deg: float
    speed_kph: float
    turn_rate_deg_s: float


def resolve_motion(
    config: Config,
    *,
    track_heading: float,
    track_speed_kph: float,
    turn_rate_deg_s: float,
    kf: KinematicKalmanFilter | None,
    for_display: bool = False,
) -> ResolvedMotion:
    """
    Build motion for curved dead reckoning / ETA.

    ``for_display=True`` uses track heading and speed only (stable map polyline).
    Alerting uses Kalman velocity when ``tracking.use_kalman_eta`` is enabled.
    Turn rate is always the shared smoothed track estimate.
    """
    heading = track_heading
    speed = max(0.0, track_speed_kph)

    if not for_display and config.tracking.use_kalman_eta and kf is not None:
        kalman_speed_mps = math.hypot(kf.vn, kf.ve)
        kalman_speed_kph = kalman_speed_mps * 3.6
        if kalman_speed_kph >= 5.0:
            speed = kalman_speed_kph
            heading = (math.degrees(math.atan2(kf.ve, kf.vn)) + 360.0) % 360.0

    return ResolvedMotion(
        heading_deg=heading,
        speed_kph=speed,
        turn_rate_deg_s=turn_rate_deg_s,
    )
