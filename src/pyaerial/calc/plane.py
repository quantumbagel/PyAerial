"""
Per-plane calculations and live geofence alerting.

Runs each tick on every tracked plane: derives speed/heading from position
history, optionally enriches callsign metadata, and fires alerters when zone
level requirements are satisfied.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import requests
from shapely import Polygon

from pyaerial.alerters import Alerter, create_alerter
from pyaerial.calc import evaluate, geo
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config.schema import Config
from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum, get_latest, patch_append

if TYPE_CHECKING:
    pass

log = logging.getLogger("pyaerial.calc.plane")

_ETA_HORIZON = 10_000


class PlaneCalculator:
    """Stateful calculator with cached alerters and optional aircraft metadata."""

    def __init__(self, config: Config, polygons: dict[str, Polygon],
                 aircraft_db: AircraftDB | None = None):
        self.config = config
        self.polygons = polygons
        self.aircraft_db = aircraft_db
        self.backdate = config.general.backdate_packets
        self._alerters: dict[tuple[str, str], Alerter] = {}

    def close(self) -> None:
        for alerter in self._alerters.values():
            alerter.close()
        self._alerters.clear()

    def calculate_all(self, planes: dict[str, dict]) -> None:
        for plane in planes.values():
            self.calculate_plane(plane)

    def calculate_plane(self, plane: dict) -> None:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_LAT not in recv or STORE_LONG not in recv:
            return

        lat_series = recv[STORE_LAT]
        lon_series = recv[STORE_LONG]
        if len(lat_series) < 2:
            return

        if len(lat_series) < self.backdate:
            previous_lat = lat_series[0]
            previous_lon = lon_series[0]
        else:
            previous_lat = lat_series[-self.backdate]
            previous_lon = get_latest(STORE_RECV_DATA, STORE_LONG, plane,
                                      previous_lat.time) or lon_series[0]

        previous = (previous_lat.value, previous_lon.value)
        previous_time = previous_lat.time
        current_lat = lat_series[-1]
        current_lon = lon_series[-1]
        current = (current_lat.value, current_lon.value)
        current_time = current_lat.time

        speed = geo.calculate_speed(previous, current, previous_time, current_time)
        heading = geo.calculate_heading(previous, current)

        final_speed, speed_time = self._choose_speed(plane, speed, current_time)
        final_heading = self._choose_heading(plane, heading, current_time)

        patch_append(plane, STORE_CALC_DATA, STORE_HORIZ_SPEED,
                     Datum(final_speed, speed_time))
        patch_append(plane, STORE_CALC_DATA, STORE_HEADING,
                     Datum(final_heading, speed_time))

        callsign = self._resolve_callsign(plane)
        self._check_alerts(plane, current, final_heading, final_speed, callsign)

    def _choose_speed(self, plane: dict, computed: float, current_time: float) -> tuple[float, float]:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HORIZ_SPEED not in recv:
            return computed, current_time
        reported = recv[STORE_HORIZ_SPEED][-1]
        if current_time - reported.time < self.backdate:
            return reported.value, reported.time
        return computed, current_time

    def _choose_heading(self, plane: dict, computed: float, current_time: float) -> float:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HEADING not in recv:
            return computed
        reported = recv[STORE_HEADING][-1]
        if current_time - reported.time < self.backdate:
            return reported.value
        return computed

    def _resolve_callsign(self, plane: dict) -> str:
        info = plane[STORE_INFO]
        if STORE_CALLSIGN in info:
            return info[STORE_CALLSIGN] or ""

        icao = info[STORE_ICAO]
        callsign = _lookup_callsign_hexdb(icao)
        if callsign is None and self.aircraft_db and self.aircraft_db.available:
            record = self.aircraft_db.lookup(icao)
            if record:
                callsign = record.get("callsign") or record.get("registration")
        info[STORE_CALLSIGN] = callsign or ""
        return info[STORE_CALLSIGN]

    def _check_alerts(self, plane: dict, position: tuple[float, float],
                      heading: float, speed: float, callsign: str) -> None:
        geofence_etas: dict[str, float] = {}

        for zone_name, zone in self.config.zones.items():
            polygon = self.polygons[zone_name]
            eta = geo.time_to_enter_geofence(position, heading, speed, polygon, _ETA_HORIZON)
            geofence_etas[zone_name] = eta

            resolver = evaluate.make_resolver(plane, eta, polygon, position)
            for level_name, level in zone.levels.items():
                if not evaluate.requirement_passes(
                        level.requirements, self.config.components, resolver):
                    continue

                category = self.config.resolve_category(level.category)
                alerter = self._get_alerter(category.alert_method, category.arguments)
                alt = get_latest(STORE_RECV_DATA, STORE_ALT, plane)
                payload = {
                    STORE_LAT: position[0],
                    STORE_LONG: position[1],
                    STORE_ALT: alt.value if alt else None,
                }
                meta = {
                    STORE_ICAO: plane[STORE_INFO][STORE_ICAO],
                    STORE_CALLSIGN: callsign,
                    ALERT_CAT_TYPE: level_name,
                    ALERT_CAT_ZONE: zone_name,
                    ALERT_CAT_ETA: eta,
                    ALERT_CAT_REASON: {
                        "zones": geofence_etas,
                        "category": level.category if isinstance(level.category, str)
                        else level.category.alert_method,
                    },
                }
                alerter.alert(meta, payload)

    def _get_alerter(self, method: str, arguments: dict) -> Alerter:
        key = (method, str(sorted(arguments.items())))
        if key not in self._alerters:
            self._alerters[key] = create_alerter(method, arguments)
        return self._alerters[key]


def _lookup_callsign_hexdb(icao: str) -> str | None:
    try:
        resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao}", timeout=1)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data.get("Registration")
