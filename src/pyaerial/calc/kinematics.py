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
    MAX_COAST_SECONDS,
    MAX_KALMAN_DT,
    MIN_SPEED_DT,
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

        prev_speed_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HORIZ_SPEED, [])
        prev_heading_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HEADING, [])
        previous_calc_speed = prev_speed_series[-1].value if prev_speed_series else None
        previous_calc_heading = (
            prev_heading_series[-1].value if prev_heading_series else None
        )

        speed: float | None
        heading: float | None
        if len(lat_series) < 2:
            speed = previous_calc_speed
            heading = previous_calc_heading
            previous_time = current_time
        else:
            # backdate_packets=1 would compare the current sample to itself.
            steps = max(int(self.backdate), 2)
            if len(lat_series) < steps:
                previous_lat = lat_series[0]
            else:
                previous_lat = lat_series[-steps]
            previous_lon = (
                get_latest(STORE_RECV_DATA, STORE_LONG, plane, previous_lat.time)
                or lon_series[0]
            )
            previous = (previous_lat.value, previous_lon.value)
            previous_time = previous_lat.time
            computed = geo.calculate_speed(
                previous, current, previous_time, current_time
            )
            speed = computed if computed is not None else previous_calc_speed
            if current_time - previous_time < MIN_SPEED_DT:
                heading = previous_calc_heading
            else:
                heading = geo.calculate_heading(previous, current)

        final_speed, speed_time = self._choose_speed(plane, speed, current_time)
        final_heading = self._choose_heading(plane, heading, current_time)

        if prev_speed_series and final_speed is not None:
            final_speed = (
                _SPEED_SMOOTH_ALPHA * final_speed
                + (1.0 - _SPEED_SMOOTH_ALPHA) * prev_speed_series[-1].value
            )

        if prev_heading_series and final_heading is not None:
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
            dt_kf = min(max(0.0, current_time - kf.last_update_time), MAX_KALMAN_DT)
            kf.update(current[0], current[1], dt_kf)
            kf.last_update_time = current_time

        turn_ref = heading if heading is not None else (final_heading or 0.0)
        turn_now, turn_then, turn_dt = self._turn_headings(
            plane, turn_ref, current_time, lat_series
        )
        prev_turn = self._smoothed_turn_rates.get(icao)
        smoothed_turn = estimate_turn_rate_deg_s(
            turn_now,
            turn_then,
            turn_dt,
            prev_smoothed=prev_turn,
        )
        self._smoothed_turn_rates[icao] = smoothed_turn

        motion_speed = final_speed if final_speed is not None else 0.0
        motion_heading = final_heading if final_heading is not None else 0.0
        motion = resolve_motion(
            self.config,
            track_heading=motion_heading,
            track_speed_kph=motion_speed,
            turn_rate_deg_s=smoothed_turn,
            kf=kf,
        )

        if final_speed is not None:
            patch_append(
                plane, STORE_CALC_DATA, STORE_HORIZ_SPEED, Datum(final_speed, speed_time)
            )
        if final_heading is not None:
            patch_append(
                plane, STORE_CALC_DATA, STORE_HEADING, Datum(final_heading, speed_time)
            )

        now = time.time()
        age = max(0.0, now - current_time)
        alert_position = current
        if 0.5 < age <= MAX_COAST_SECONDS and motion.speed_kph > 0:
            alert_position = geo.dead_reckon_curved(
                current,
                motion.heading_deg,
                motion.speed_kph,
                motion.turn_rate_deg_s,
                age,
            )
            motion = ResolvedMotion(
                heading_deg=(motion.heading_deg + motion.turn_rate_deg_s * age) % 360.0,
                speed_kph=motion.speed_kph,
                turn_rate_deg_s=motion.turn_rate_deg_s,
            )

        return KinematicUpdate(
            icao=icao,
            fix=current,
            alert_position=alert_position,
            motion=motion,
        )

    def _choose_speed(
        self, plane: dict, computed: float | None, current_time: float
    ) -> tuple[float | None, float]:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HORIZ_SPEED not in recv:
            return computed, current_time
        reported = recv[STORE_HORIZ_SPEED][-1]
        if current_time - reported.time < _ADS_B_TRUST_SECONDS:
            # Stamp at the position time, not the velocity-packet time. ADS-B
            # speed/heading messages are sparse; reusing their timestamp made
            # patch_append drop every subsequent sample and froze historical
            # tracks to a single speed/heading.
            return reported.value, current_time
        return computed, current_time

    def _turn_headings(
        self,
        plane: dict,
        geodesic_heading: float,
        current_time: float,
        lat_series: list,
    ) -> tuple[float, float, float]:
        """Turn rate from one heading source: successive ADS-B tracks, else last two geodesics."""
        recv = plane.get(STORE_RECV_DATA, {})
        adsb = recv.get(STORE_HEADING) or []
        if (
            len(adsb) >= 2
            and current_time - adsb[-1].time < _ADS_B_TRUST_SECONDS
        ):
            return (
                adsb[-1].value,
                adsb[-2].value,
                max(adsb[-1].time - adsb[-2].time, 0.0),
            )
        if len(lat_series) >= 3:
            def _point(idx: int) -> tuple[float, float] | None:
                lon = get_latest(
                    STORE_RECV_DATA, STORE_LONG, plane, lat_series[idx].time
                )
                if lon is None:
                    return None
                return (lat_series[idx].value, lon.value)

            p0, p1, p2 = _point(-3), _point(-2), _point(-1)
            if p0 is not None and p1 is not None and p2 is not None:
                return (
                    geo.calculate_heading(p1, p2),
                    geo.calculate_heading(p0, p1),
                    max(lat_series[-1].time - lat_series[-2].time, 0.0),
                )
        return geodesic_heading, geodesic_heading, 0.0

    def _choose_heading(
        self, plane: dict, computed: float | None, current_time: float
    ) -> float | None:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HEADING not in recv:
            return computed
        reported = recv[STORE_HEADING][-1]
        if current_time - reported.time < _ADS_B_TRUST_SECONDS:
            return reported.value
        return computed
