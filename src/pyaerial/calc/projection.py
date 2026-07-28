"""Map projection polylines (track extrapolation and TC 29 intent paths)."""
from __future__ import annotations

import math
from typing import Any

from pyaerial.calc import geo
from pyaerial.calc.kalman import KinematicKalmanFilter
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_RECV_DATA,
    STORE_SELECTED_ALTITUDE,
    STORE_SELECTED_HEADING,
)


def _sample_track_path(
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
    turn_rate: float,
    *,
    horizon_seconds: float,
    step_seconds: float,
    curved: bool,
) -> list[list[float]]:
    if speed_kph <= 0 or horizon_seconds <= 0 or step_seconds <= 0:
        return [[position[0], position[1]]]

    points: list[list[float]] = [[position[0], position[1]]]
    t = step_seconds
    while t <= horizon_seconds + 1e-9:
        if curved and abs(turn_rate) >= 0.05:
            lat, lon = geo.dead_reckon_curved(position, heading, speed_kph, turn_rate, t)
        else:
            lat, lon = geo.dead_reckon_curved(position, heading, speed_kph, 0.0, t)
        points.append([lat, lon])
        t += step_seconds
    return points


def _intent_turn_params(
    heading: float,
    turn_rate: float,
    selected_heading: float,
) -> tuple[float, float]:
    """Return ``(turn_duration_seconds, effective_turn_rate_deg_per_s)``."""
    delta = (selected_heading - heading + 540.0) % 360.0 - 180.0
    if abs(delta) < 1.0:
        return 0.0, 0.0

    if abs(turn_rate) >= 0.5:
        effective_rate = turn_rate
    else:
        effective_rate = 3.0 if delta > 0 else -3.0

    if (delta > 0 and effective_rate < 0) or (delta < 0 and effective_rate > 0):
        effective_rate = -effective_rate

    return abs(delta / effective_rate), effective_rate


def _position_along_intent(
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
    turn_rate: float,
    selected_heading: float,
    elapsed: float,
) -> tuple[float, float]:
    if elapsed <= 0 or speed_kph <= 0:
        return position

    turn_time, effective_rate = _intent_turn_params(heading, turn_rate, selected_heading)
    if turn_time <= 0:
        return geo.dead_reckon_curved(position, selected_heading, speed_kph, 0.0, elapsed)

    if elapsed <= turn_time:
        return geo.dead_reckon_curved(position, heading, speed_kph, effective_rate, elapsed)

    turn_end = geo.dead_reckon_curved(position, heading, speed_kph, effective_rate, turn_time)
    return geo.dead_reckon_curved(turn_end, selected_heading, speed_kph, 0.0, elapsed - turn_time)


def _sample_intent_path(
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
    turn_rate: float,
    selected_heading: float,
    *,
    horizon_seconds: float,
    step_seconds: float,
) -> list[list[float]]:
    if speed_kph <= 0 or horizon_seconds <= 0 or step_seconds <= 0:
        return [[position[0], position[1]]]

    points: list[list[float]] = [[position[0], position[1]]]
    t = step_seconds
    while t <= horizon_seconds + 1e-9:
        lat, lon = _position_along_intent(
            position, heading, speed_kph, turn_rate, selected_heading, t,
        )
        points.append([lat, lon])
        t += step_seconds
    return points


def build_portal_projection(
    plane: dict,
    config: Config,
    kf: KinematicKalmanFilter | None,
    position: tuple[float, float],
    heading: float,
    speed_kph: float,
) -> dict[str, Any]:
    """
    Build forward paths for the web map using the same motion inputs as ETA logic.
    """
    tracking = config.tracking
    horizon = tracking.projection_seconds
    step = tracking.projection_step_seconds
    curved = tracking.curved_projection

    eta_heading = heading
    eta_speed = speed_kph
    turn_rate = kf.turn_rate if kf is not None else 0.0

    if tracking.use_kalman_eta and kf is not None:
        kalman_speed_mps = math.hypot(kf.vn, kf.ve)
        kalman_speed_kph = kalman_speed_mps * 3.6
        if kalman_speed_kph >= 5.0:
            eta_speed = kalman_speed_kph
            eta_heading = (math.degrees(math.atan2(kf.ve, kf.vn)) + 360.0) % 360.0

    track_path = _sample_track_path(
        position,
        eta_heading,
        eta_speed,
        turn_rate,
        horizon_seconds=horizon,
        step_seconds=step,
        curved=curved,
    )

    recv = plane.get(STORE_RECV_DATA, {})
    sel_heading_series = recv.get(STORE_SELECTED_HEADING, [])
    sel_alt_series = recv.get(STORE_SELECTED_ALTITUDE, [])
    selected_heading: float | None = None
    selected_altitude: float | None = None
    intent_path: list[list[float]] | None = None

    if sel_heading_series:
        selected_heading = float(sel_heading_series[-1].value)
        intent_path = _sample_intent_path(
            position,
            eta_heading,
            eta_speed,
            turn_rate,
            selected_heading,
            horizon_seconds=horizon,
            step_seconds=step,
        )
    if sel_alt_series:
        selected_altitude = float(sel_alt_series[-1].value)

    return {
        "horizon_seconds": horizon,
        "step_seconds": step,
        "track_path": track_path,
        "intent_path": intent_path,
        "selected_heading": selected_heading,
        "selected_altitude": selected_altitude,
        "motion_heading": eta_heading,
        "motion_speed_kph": eta_speed,
        "turn_rate_deg_s": turn_rate,
    }