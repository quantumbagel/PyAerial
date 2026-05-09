"""
Filtering and persistence (MongoDB) for PyAerial.
"""
from __future__ import annotations

import logging
import math

import pymongo
from geopy.distance import geodesic
from pymongo.errors import PyMongoError
from shapely import Point, Polygon
from shapely.ops import nearest_points

from pyaerial import calculations
from pyaerial.constants import (
    ALERT_CAT_ETA,
    CONFIGURATION,
    CONFIG_CAT_SAVE,
    CONFIG_CATEGORIES,
    CONFIG_CAT_DEFAULT_SAVE_METHOD,
    CONFIG_CAT_SAVE_METHOD_ALL,
    CONFIG_CAT_SAVE_METHOD_DECIMATE,
    CONFIG_CAT_SAVE_METHOD_NONE,
    CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE,
    CONFIG_COMP_FUNCTIONS,
    CONFIG_COMPONENTS,
    CONFIG_ZONES,
    CONFIG_ZONES_COORDINATES,
    CONFIG_ZONES_LEVELS,
    CONFIG_ZONES_LEVELS_CATEGORY,
    CONFIG_ZONES_LEVELS_REQUIREMENTS,
    CONFIG_ZONES_LEVELS_SECONDS,
    STORE_CALC_DATA,
    STORE_DATA_CONFIG_NAMING,
    STORE_DATA_TYPES,
    STORE_DISTANCE,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_FIRST_PACKET,
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
    STORE_VERT_SPEED,
    STORAGE_CATEGORY,
    STORAGE_DATA,
    STORAGE_DATA_TYPE,
    STORAGE_LEVEL,
    STORAGE_ZONE,
)
from pyaerial.helpers import Datum
from pyaerial.requirements_eval import collect_component_names, eval_requirement


def filter_packets(packets, method=CONFIG_CAT_SAVE_METHOD_ALL):
    if method == CONFIG_CAT_SAVE_METHOD_ALL:
        return packets
    if method.startswith(CONFIG_CAT_SAVE_METHOD_DECIMATE):
        n = int(
            method.replace(CONFIG_CAT_SAVE_METHOD_DECIMATE, "")
            .replace(" ", "")
            .replace("(", "")
            .replace(")", "")
        )
        return [p for i, p in enumerate(packets) if (i % n) == 0]
    if method.startswith(CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE):
        arg = tuple(
            float(i)
            for i in method.replace(CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE, "")
            .replace("(", "")
            .replace(")", "")
            .replace(" ", "")
            .split(",")
        )
        reset_timestamp = packets[0][1] + arg[1]
        return_packets = []
        window_population_size = 0
        for packet in packets:
            if packet[1] < reset_timestamp and window_population_size < arg[0]:
                window_population_size += 1
                return_packets.append(packet)
            if window_population_size >= arg[0] and reset_timestamp < packet[1]:
                window_population_size = 0
                reset_timestamp = packet[1] + arg[1]
        return return_packets
    if method == CONFIG_CAT_SAVE_METHOD_NONE:
        return []
    return packets


class Saver:
    def __init__(self, log_name: str = "Saver") -> None:
        self.logger = logging.getLogger(name=log_name)
        self._cache = {}

    def add_plane_to_cache(self, plane_id: str, zone: str, level: str, cache: dict[str, list[Datum]]) -> None:
        self._cache[(plane_id, zone, level)] = cache

    def save(self):
        raise NotImplementedError

    def cache_flight(self, plane):
        information = plane[STORE_INFO]
        internal_information = plane[STORE_INTERNAL]
        first_time = plane[STORE_INTERNAL][STORE_FIRST_PACKET]
        last_time = plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET]
        received_information = plane[STORE_RECV_DATA]
        calculated_information = plane[STORE_CALC_DATA]

        if STORE_LAT not in received_information or STORE_HEADING not in calculated_information:
            self.logger.getChild("cache_flight").warning(
                "Plane %s did not have heading and/or position information; ignoring",
                plane[STORE_INFO][STORE_ICAO],
            )
            return False

        saved = False
        for zone_name in CONFIGURATION[CONFIG_ZONES]:
            levels = CONFIGURATION[CONFIG_ZONES][zone_name][CONFIG_ZONES_LEVELS]
            zone_coords = CONFIGURATION[CONFIG_ZONES][zone_name][CONFIG_ZONES_COORDINATES]

            for level in levels:
                category = levels[level][CONFIG_ZONES_LEVELS_CATEGORY]
                if isinstance(category, str):
                    category = CONFIGURATION[CONFIG_CATEGORIES][category]
                minimum_eta = math.inf
                total_valid_ticks = 0

                for tick in range(int(first_time + 1), int(last_time) + 1):
                    latitude_datum = calculations.get_latest(STORE_RECV_DATA, STORE_LAT, plane, tick)
                    longitude_datum = calculations.get_latest(STORE_RECV_DATA, STORE_LONG, plane, tick)
                    latest_direction = calculations.get_latest(STORE_CALC_DATA, STORE_HEADING, plane, tick)
                    latest_speed = calculations.get_latest(
                        STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, tick
                    )
                    if not latitude_datum or not longitude_datum or not latest_direction or not latest_speed:
                        continue

                    eta = calculations.time_to_enter_geofence(
                        [latitude_datum.value, longitude_datum.value],
                        latest_direction.value,
                        latest_speed.value,
                        zone_coords,
                        100000,
                    )
                    if eta < minimum_eta:
                        minimum_eta = eta

                    requirements = levels[level][CONFIG_ZONES_LEVELS_REQUIREMENTS]
                    component_names = collect_component_names(requirements)
                    components = {}
                    for component_name in component_names:
                        component_failed = False
                        component = CONFIGURATION[CONFIG_COMPONENTS][component_name]
                        for data_type in component:
                            relevant_data = None

                            if data_type in (STORE_LAT, STORE_LONG, STORE_ALT, STORE_VERT_SPEED):
                                ld = calculations.get_latest(STORE_RECV_DATA, data_type, plane, tick)
                                relevant_data = ld.value if ld else None
                            elif data_type in (STORE_HORIZ_SPEED, STORE_HEADING):
                                ld = calculations.get_latest(STORE_CALC_DATA, data_type, plane, tick)
                                relevant_data = ld.value if ld else None
                            elif data_type == ALERT_CAT_ETA:
                                relevant_data = eta
                            elif data_type == STORE_DISTANCE:
                                points = nearest_points(
                                    Polygon(zone_coords),
                                    Point([latitude_datum.value, longitude_datum.value]),
                                )
                                pt = points[0]
                                relevant_data = geodesic(
                                    (pt.x, pt.y),
                                    (latitude_datum.value, longitude_datum.value),
                                ).km

                            if relevant_data is None:
                                component_failed = True
                                break
                            for comparison in component[data_type]:
                                if not CONFIG_COMP_FUNCTIONS[comparison](
                                    relevant_data, component[data_type][comparison]
                                ):
                                    component_failed = True
                                    break
                            if component_failed:
                                break
                        components[component_name] = not component_failed

                    if eval_requirement(requirements, components):
                        total_valid_ticks += 1

                if total_valid_ticks >= levels[level][CONFIG_ZONES_LEVELS_SECONDS]:
                    all_filtered_information = {STORE_INTERNAL: internal_information, STORE_INFO: information}
                    for type_of_information in STORE_DATA_TYPES:
                        all_filtered_information[type_of_information] = {}
                        configuration_saving_category = STORE_DATA_CONFIG_NAMING[type_of_information]
                        save_block = category[CONFIG_CAT_SAVE][configuration_saving_category]
                        for subcategory in STORE_DATA_TYPES[type_of_information]:
                            if subcategory in save_block:
                                method = save_block[subcategory]
                            else:
                                method = save_block[CONFIG_CAT_DEFAULT_SAVE_METHOD]
                            src = plane[type_of_information].get(subcategory, [])
                            filtered = filter_packets(src, method)
                            all_filtered_information[type_of_information][subcategory] = filtered

                    self.add_plane_to_cache(
                        plane[STORE_INFO][STORE_ICAO], zone_name, level, all_filtered_information
                    )
                    saved = True
        return saved


class PrintSaver(Saver):
    def __init__(self):
        super().__init__(log_name="print")

    def save(self):
        self.logger.info("SAVING: %s", self._cache)
        self._cache = {}


class MongoSaver(Saver):
    def __init__(self, uri):
        super().__init__(log_name="mongodb")
        self.database: pymongo.MongoClient | None = None
        self.uri = uri
        self.connect_to_database()

    def connect_to_database(self):
        self.database = pymongo.MongoClient(
            self.uri, serverSelectionTimeoutMS=2000, connectTimeoutMS=1000, socketTimeoutMS=1000
        )
        try:
            self.database.admin.command("ismaster")
        except PyMongoError:
            self.logger.error("Disconnected from MongoDB; reconnecting (uri=%s)", self.uri)
            while True:
                try:
                    self.database = pymongo.MongoClient(
                        self.uri,
                        serverSelectionTimeoutMS=2000,
                        connectTimeoutMS=1000,
                        socketTimeoutMS=1000,
                    )
                    self.database.admin.command("ismaster")
                except PyMongoError:
                    self.logger.warning("Mongo reconnect failed (uri=%s)", self.uri)
                    continue
                self.logger.info("Reconnected to MongoDB")
                break

    def save(self):
        self.logger.info(
            "Saving cache (%s flight-levels)", len(self._cache),
        )
        for flight in self._cache:
            icao = flight[0]
            zone = flight[1]
            level = flight[2]
            data = self._cache[flight]
            data[STORE_INTERNAL][STORE_PACKET_TYPE] = {
                str(i): data[STORE_INTERNAL][STORE_PACKET_TYPE][i]
                for i in data[STORE_INTERNAL][STORE_PACKET_TYPE]
            }
            database = self.database.get_database(icao.lower())
            try:
                collection = database.get_collection(
                    str(int(data[STORE_INTERNAL][STORE_FIRST_PACKET])) + "-" + zone + "-" + level
                )
            except PyMongoError:
                self.connect_to_database()
                collection = database.get_collection(
                    str(int(data[STORE_INTERNAL][STORE_FIRST_PACKET])) + "-" + zone + "-" + level
                )

            for data_type in (STORE_RECV_DATA, STORE_CALC_DATA):
                for item in data[data_type]:
                    document = {
                        STORAGE_CATEGORY: data_type,
                        STORAGE_DATA_TYPE: item,
                        STORAGE_DATA: [[datum.time, datum.value] for datum in data[data_type][item]],
                    }
                    try:
                        collection.insert_one(document)
                    except PyMongoError:
                        self.connect_to_database()

            document = {STORAGE_CATEGORY: STORE_INFO, STORAGE_ZONE: zone, STORAGE_LEVEL: level}
            for info_type in (STORE_INFO, STORE_INTERNAL):
                document.update({str(i): data[info_type][i] for i in data[info_type]})
            try:
                collection.insert_one(document)
            except PyMongoError:
                self.connect_to_database()

        self.logger.info("Done saving %s flight-levels", len(self._cache))
        self._cache = {}
