"""
Saver plugins: persistence backends for eligible flights.

A saver decides whether a completed flight qualifies for any zone level
(:meth:`Saver.cache_flight`), filters its data per the matched category's save
rules, and later flushes the cache to a backend (:meth:`Saver.save`).
"""
from __future__ import annotations

import abc
import logging
import math
from typing import Callable

from shapely import Polygon

from pyaerial.calc import evaluate, geo
from pyaerial.config.schema import CategoryConfig, Config
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_DATA_CONFIG_NAMING,
    STORE_DATA_TYPES,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
    CONFIG_CAT_DEFAULT_SAVE_METHOD,
)
from pyaerial.models import get_latest
from pyaerial.save_methods import filter_packets

_REGISTRY: dict[str, type["Saver"]] = {}

# Cartesian ETA search horizon (large; the exact value does not matter).
_ETA_HORIZON = 100_000


class Saver(abc.ABC):
    """Base class for savers."""

    def __init__(self, config: Config, polygons: dict[str, Polygon]):
        self.config = config
        self.polygons = polygons
        self.logger = logging.getLogger(f"pyaerial.saver.{self.method}")
        self._cache: dict[tuple[str, str, str], dict] = {}

    #: Registered name; set by :func:`register_saver`.
    method: str = "base"

    def add_plane_to_cache(self, plane_id: str, zone: str, level: str, data: dict) -> None:
        self._cache[(plane_id, zone, level)] = data

    def cache_flight(self, plane: dict) -> bool:
        """Evaluate a finished flight against every zone/level and cache matches."""
        recv = plane[STORE_RECV_DATA]
        calc = plane[STORE_CALC_DATA]
        internal = plane[STORE_INTERNAL]
        icao = plane[STORE_INFO][STORE_ICAO]

        if STORE_LAT not in recv or STORE_HEADING not in calc:
            self.logger.debug(
                "Plane %s lacks position/heading data; cannot infer importance. Skipping.", icao)
            return False

        first_time = internal[STORE_FIRST_PACKET]
        last_time = internal[STORE_MOST_RECENT_PACKET]
        saved = False

        for zone_name, zone in self.config.zones.items():
            polygon = self.polygons[zone_name]
            for level_name, level in zone.levels.items():
                category = self.config.resolve_category(level.category)
                if self._count_valid_ticks(plane, zone_name, polygon, level.requirements,
                                           first_time, last_time) >= level.seconds:
                    self.add_plane_to_cache(icao, zone_name, level_name,
                                            self._filter_flight(plane, category))
                    saved = True
        return saved

    def _count_valid_ticks(self, plane: dict, zone_name: str, polygon: Polygon,
                           requirement: str, first_time: float, last_time: float) -> int:
        valid = 0
        for tick in range(int(first_time) + 1, int(last_time) + 1):
            lat = get_latest(STORE_RECV_DATA, STORE_LAT, plane, tick)
            lon = get_latest(STORE_RECV_DATA, STORE_LONG, plane, tick)
            heading = get_latest(STORE_CALC_DATA, STORE_HEADING, plane, tick)
            speed = get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, tick)
            if None in (lat, lon, heading, speed):
                continue
            position = (lat.value, lon.value)
            eta = geo.time_to_enter_geofence(position, heading.value, speed.value,
                                             polygon, _ETA_HORIZON)
            if eta is math.inf:
                eta = math.inf
            resolver = evaluate.make_resolver(plane, eta, polygon, position, tick)
            if evaluate.requirement_passes(requirement, self.config.components, resolver):
                valid += 1
        return valid

    def _filter_flight(self, plane: dict, category: CategoryConfig) -> dict:
        filtered = {
            STORE_INTERNAL: plane[STORE_INTERNAL],
            STORE_INFO: plane[STORE_INFO],
        }
        for bucket, fields in STORE_DATA_TYPES.items():
            save_group = getattr(category.save, STORE_DATA_CONFIG_NAMING[bucket])
            filtered[bucket] = {}
            for field in fields:
                if field not in plane.get(bucket, {}):
                    continue
                method = save_group.get(field, save_group.get(CONFIG_CAT_DEFAULT_SAVE_METHOD, "all"))
                filtered[bucket][field] = filter_packets(plane[bucket][field], method)
        return filtered

    @abc.abstractmethod
    def save(self) -> None:
        """Persist and clear the cache."""

    def close(self) -> None:
        """Release any held resources. Override as needed."""


def register_saver(name: str) -> Callable[[type[Saver]], type[Saver]]:
    def decorator(cls: type[Saver]) -> type[Saver]:
        cls.method = name
        _REGISTRY[name] = cls
        return cls
    return decorator


def available_savers() -> list[str]:
    return sorted(_REGISTRY)


def create_saver(method: str, config: Config, polygons: dict[str, Polygon]) -> Saver:
    if method not in _REGISTRY:
        raise KeyError(f"unknown saver {method!r}; available: {available_savers()}")
    return _REGISTRY[method](config, polygons)


from pyaerial.savers import mongo as _mongo  # noqa: E402,F401
from pyaerial.savers import printer as _printer  # noqa: E402,F401
