"""Per-plane speed, heading, Kalman, and dead-reckoned alert position."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from pyaerial.calc import geo
from pyaerial.calc.kalman import KinematicKalmanFilter
from pyaerial.calc.motion import (
    ResolvedMotion,
    estimate_turn_rate_deg_s,
    resolve_motion,
)
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum, get_latest, patch_append

_ADS_B_TRUST_SECONDS = 10.0
_HEADING_SMOOTH_ALPHA = 0.3
_SPEED_SMOOTH_ALPHA = 0.3


@dataclass(slots=True)
class KinematicUpdate:
    """Motion snapshot used by alerting after a kinematics tick."""

    icao: str
    fix: tuple[float, float]
    alert_position: tuple[float, float]
    motion: ResolvedMotion


class Kinematics:
    """Stateful speed/heading smoother and Kalman filters, keyed by ICAO."""

    def __init__(self, config: Config):
        self.config = config
        self.backdate = config.tracking.backdate_packets
        self._kalman_filters: dict[str, KinematicKalmanFilter] = {}
        self._smoothed_turn_rates: dict[str, float] = {}

    def close(self) -> None:
        self._kalman_filters.clear()
        self._smoothed_turn_rates.clear()

    def forget(self, icao: str) -> None:
        """Drop Kalman / turn-rate state so the next flight of this ICAO starts clean."""
        key = icao.lower()
        self._kalman_filters.pop(key, None)
        self._smoothed_turn_rates.pop(key, None)

    def update(self, plane: dict) -> KinematicUpdate | None:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_LAT not in recv or STORE_LONG not in recv:
            return None

        lat_series = recv[STORE_LAT]
        lon_series = recv[STORE_LONG]
        if not lat_series or not lon_series:
            return None

        current_lat = lat_series[-1]
        current_lon = (
            get_latest(STORE_RECV_DATA, STORE_LONG, plane, current_lat.time)
            or lon_series[-1]
        )
        current = (current_lat.value, current_lon.value)
        current_time = current_lat.time

        if len(lat_series) < 2:
            speed = 0.0
            heading = 0.0
            previous_time = current_time
        else:
            if len(lat_series) < self.backdate:
                previous_lat = lat_series[0]
            else:
                previous_lat = lat_series[-self.backdate]
            previous_lon = (
                get_latest(STORE_RECV_DATA, STORE_LONG, plane, previous_lat.time)
                or lon_series[0]
            )
            previous = (previous_lat.value, previous_lon.value)
            previous_time = previous_lat.time
            speed = geo.calculate_speed(previous, current, previous_time, current_time)
            heading = geo.calculate_heading(previous, current)

        final_speed, speed_time = self._choose_speed(plane, speed, current_time)
        final_heading = self._choose_heading(plane, heading, current_time)

        prev_speed_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HORIZ_SPEED, [])
        if prev_speed_series:
            final_speed = (
                _SPEED_SMOOTH_ALPHA * final_speed
                + (1.0 - _SPEED_SMOOTH_ALPHA) * prev_speed_series[-1].value
            )

        prev_heading_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HEADING, [])
        if prev_heading_series:
            prev_heading = prev_heading_series[-1].value
            rad_current = math.radians(final_heading)
            rad_prev = math.radians(prev_heading)
            sin_val = (
                _HEADING_SMOOTH_ALPHA * math.sin(rad_current)
                + (1.0 - _HEADING_SMOOTH_ALPHA) * math.sin(rad_prev)
            )
            cos_val = (
                _HEADING_SMOOTH_ALPHA * math.cos(rad_current)
                + (1.0 - _HEADING_SMOOTH_ALPHA) * math.cos(rad_prev)
            )
            final_heading = (math.degrees(math.atan2(sin_val, cos_val)) + 360.0) % 360.0

        icao = plane[STORE_INFO][STORE_ICAO].lower()
        kf = self._kalman_filters.get(icao)
        if kf is None:
            kf = KinematicKalmanFilter(current[0], current[1])
            self._kalman_filters[icao] = kf
            kf.last_update_time = current_time
        elif current_time > kf.last_update_time:
            prev_fix = lat_series[-2] if len(lat_series) >= 2 else current_lat
            dt_kf = min(max(0.0, current_time - prev_fix.time), 30.0)
            kf.update(current[0], current[1], dt_kf)
            kf.last_update_time = current_time

        window_dt = max(current_time - previous_time, 0.0)
        window_start_heading = heading
        lat_idx = (
            0 if len(lat_series) < self.backdate else len(lat_series) - self.backdate
        )
        if lat_idx >= 1:
            lon_at = get_latest(
                STORE_RECV_DATA, STORE_LONG, plane, lat_series[lat_idx].time
            )
            lon_before = get_latest(
                STORE_RECV_DATA,
                STORE_LONG,
                plane,
                lat_series[lat_idx - 1].time,
            )
            if lon_at is not None and lon_before is not None:
                p0 = (lat_series[lat_idx - 1].value, lon_before.value)
                p1 = (lat_series[lat_idx].value, lon_at.value)
                window_start_heading = geo.calculate_heading(p0, p1)

        prev_turn = self._smoothed_turn_rates.get(icao)
        smoothed_turn = estimate_turn_rate_deg_s(
            final_heading,
            window_start_heading,
            window_dt,
            prev_smoothed=prev_turn,
        )
        self._smoothed_turn_rates[icao] = smoothed_turn

        motion = resolve_motion(
            self.config,
            track_heading=final_heading,
            track_speed_kph=final_speed,
            turn_rate_deg_s=smoothed_turn,
            kf=kf,
        )

        patch_append(
            plane, STORE_CALC_DATA, STORE_HORIZ_SPEED, Datum(final_speed, speed_time)
        )
        patch_append(
            plane, STORE_CALC_DATA, STORE_HEADING, Datum(final_heading, speed_time)
        )

        now = time.time()
        age = min(max(0.0, now - current_time), self.config.tracking.remember_planes)
        alert_position = current
        if age > 0.5 and motion.speed_kph > 0:
            alert_position = geo.dead_reckon_curved(
                current,
                motion.heading_deg,
                motion.speed_kph,
                motion.turn_rate_deg_s,
                age,
            )

        return KinematicUpdate(
            icao=icao,
            fix=current,
            alert_position=alert_position,
            motion=motion,
        )

    def _choose_speed(
        self, plane: dict, computed: float, current_time: float
    ) -> tuple[float, float]:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HORIZ_SPEED not in recv:
            return computed, current_time
        reported = recv[STORE_HORIZ_SPEED][-1]
        if current_time - reported.time < _ADS_B_TRUST_SECONDS:
            return reported.value, reported.time
        return computed, current_time

    def _choose_heading(
        self, plane: dict, computed: float, current_time: float
    ) -> float:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HEADING not in recv:
            return computed
        reported = recv[STORE_HEADING][-1]
        if current_time - reported.time < _ADS_B_TRUST_SECONDS:
            return reported.value
        return computed
