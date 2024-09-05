"""
Rosetta is PyAerial's module for filtering and saving to the database.
"""
import ast
import logging
import math

import pymongo
from geopy.distance import geodesic
from pymongo.errors import NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError, AutoReconnect, PyMongoError
from shapely import Polygon, Point
from shapely.ops import nearest_points

import calculations
from constants import *
from helpers import Datum


def filter_packets(packets, method=CONFIG_CAT_SAVE_METHOD_ALL):
    """
    Filter packets using one of four methods:

    all: return all packets
    none: return no packets
    decimate: remove all but every nth packet
    sdecimate: allow a maximum of x packets every y seconds

    :param packets: The packets to filter
    :param method: method for filtering
    :return: the filtered packets
    """
    if method == CONFIG_CAT_SAVE_METHOD_ALL:
        return packets
    elif method.startswith(CONFIG_CAT_SAVE_METHOD_DECIMATE):
        return [p for i, p in enumerate(packets) if
                (i % int(method.replace(CONFIG_CAT_SAVE_METHOD_DECIMATE, "")
                        .replace(' ', "").replace("(", "").replace(")", ""))) == 0]
    elif method.startswith(CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE):
        arg = tuple([float(i) for i in method
                    .replace(CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE, "")
                    .replace("(", "")
                    .replace(")", "")
                    .replace(' ', "")
                    .split(",")])
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


class Saver:
    def __init__(self, log_name: str = "Saver") -> None:
        """
        Initialize a Saver.
        :param log_name: name of the logger
        """
        self.logger = logging.getLogger(name=log_name)
        self._cache = {}

    def add_plane_to_cache(self, plane_id: str, zone: str, level: str, cache: dict[str, list[Datum]]) -> None:
        """
        Adds a plane's data to the cache. Assumes data is valid.

        :param plane_id: The ID of the plane
        :param zone: The name of the zone
        :param level: The level within the zone the plane has sated
        :param cache: the plane's data to save
        """
        self._cache[(plane_id, zone, level)] = cache

    def save(self):
        """
        Save the data in self._cache using whatever method was implemented by the child class.
        This method is also expected to clear the cache variable
        """
        raise NotImplementedError

    def cache_flight(self, plane):
        """
        Filters packets, adds them to the Saver cache, and requests the Saver to save the information
        :param plane: The plane data to parse and save
        """
        information = plane[STORE_INFO]
        calculated_information = plane[STORE_CALC_DATA]
        received_information = plane[STORE_RECV_DATA]
        internal_information = plane[STORE_INTERNAL]
        first_time = plane[STORE_INTERNAL][STORE_FIRST_PACKET]
        last_time = plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET]
        # Ensure importance
        if STORE_LAT not in received_information.keys() or STORE_HEADING not in calculated_information.keys():
            # Not important enough LMAO
            self.logger.getChild("cache_flight").warning(f"Plane {plane[STORE_INFO][STORE_ICAO]}"
                                                         f" did not have heading and/or position information,"
                                                         f" can't infer importance. Ignoring")
            return

        for zone in CONFIGURATION[CONFIG_ZONES]:
            levels = CONFIGURATION[CONFIG_ZONES][zone][CONFIG_ZONES_LEVELS]
            for level in levels:
                category = levels[level][CONFIG_ZONES_LEVELS_CATEGORY]
                if type(category) is str:
                    category = CONFIGURATION[CONFIG_CATEGORIES][category]
                minimum_eta = math.inf
                total_valid_ticks = 0

                for time in range(int(first_time + 1), int(last_time) + 1):
                    latitude_datum = calculations.get_latest(STORE_RECV_DATA, STORE_LAT, plane,
                                                             time)
                    longitude_datum = calculations.get_latest(STORE_RECV_DATA, STORE_LONG, plane,
                                                              time)
                    latest_direction = calculations.get_latest(STORE_CALC_DATA, STORE_HEADING, plane,
                                                               time)
                    latest_speed = calculations.get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane,
                                                           time)
                    eta = calculations.time_to_enter_geofence([latitude_datum.value, longitude_datum.value],
                                                              latest_direction.value,
                                                              latest_speed.value,
                                                              CONFIGURATION[CONFIG_ZONES][zone][
                                                                  CONFIG_ZONES_COORDINATES],
                                                              100000)
                    if eta < minimum_eta:
                        minimum_eta = eta
                    requirements = levels[level][CONFIG_ZONES_LEVELS_REQUIREMENTS]
                    component_names = [node.id for node in ast.walk(ast.parse(requirements))
                                       if type(node) is ast.Name]
                    components = {}
                    for component_name in component_names:
                        component_failed = False
                        component = CONFIGURATION[CONFIG_COMPONENTS][component_name]  # The data within the component
                        for data_type in component.keys():  # For each data type, find the piece of data that's relevant
                            relevant_data = None

                            if data_type in [STORE_LAT, STORE_LONG, STORE_ALT, STORE_VERT_SPEED]:  # Received data
                                relevant_data = calculations.get_latest(STORE_RECV_DATA, data_type, plane, time).value
                            elif data_type in [STORE_HORIZ_SPEED, STORE_HEADING]:  # Calculated data
                                relevant_data = calculations.get_latest(STORE_CALC_DATA, data_type, plane, time).value
                            elif data_type == ALERT_CAT_ETA:  # ETA
                                relevant_data = eta
                            elif data_type == STORE_DISTANCE:  # Distance
                                points = nearest_points(Polygon(zone[CONFIG_ZONES_COORDINATES]),
                                                        Point([latitude_datum.value, longitude_datum.value]))
                                relevant_data = geodesic((points[0].latitude, points[0].longitude),
                                                         [latitude_datum.value, longitude_datum.value])

                            if relevant_data is None:  # If there isn't valid data, fail the component
                                component_failed = True
                                break
                            for comparison in component[data_type].keys():
                                # Has our component failed?
                                if not CONFIG_COMP_FUNCTIONS[comparison](relevant_data,
                                                                         component[data_type][comparison]):
                                    component_failed = True
                                    break
                            if component_failed:
                                break
                        components[component_name] = not component_failed  # Did the component succeed?
                    if eval(requirements, components):  # Evaluate
                        total_valid_ticks += 1

                if total_valid_ticks >= levels[level][CONFIG_ZONES_LEVELS_SECONDS]:
                    # Should we cache this level of this plane?
                    all_filtered_information = {STORE_INTERNAL: internal_information, STORE_INFO: information}
                    for type_of_information in STORE_DATA_TYPES.keys():
                        all_filtered_information[type_of_information] = {}
                        configuration_saving_category = STORE_DATA_CONFIG_NAMING[type_of_information]
                        for subcategory in STORE_DATA_TYPES[type_of_information]:
                            if subcategory in category[CONFIG_CAT_SAVE][configuration_saving_category].keys():
                                 # saving method exists for this
                                filtered = filter_packets(plane[type_of_information][subcategory],
                                                                           category[CONFIG_CAT_SAVE][configuration_saving_category][subcategory]
                                                                           )
                            else:
                                filtered = filter_packets(plane[type_of_information][subcategory],
                                                          category[CONFIG_CAT_SAVE][configuration_saving_category][CONFIG_CAT_DEFAULT_SAVE_METHOD])

                            all_filtered_information[type_of_information][subcategory] = filtered

                    self.add_plane_to_cache(plane[STORE_INFO][STORE_ICAO], zone, level,
                                            all_filtered_information)


class PrintSaver(Saver):
    def __init__(self):
        super().__init__(log_name="print")

    def save(self):
        self.logger.info(f"SAVING: {self._cache}")
        self._cache = {}


class MongoSaver(Saver):
    def __init__(self, uri):
        super().__init__(log_name="mongodb")
        self.database: pymongo.MongoClient | None = None
        self.uri = uri
        self.connect_to_database()

    def connect_to_database(self):
        """
        Connects to the MongoDB database.
        """
        self.database = pymongo.MongoClient(self.uri, serverSelectionTimeoutMS=2000,
                                            connectTimeoutMS=1000, socketTimeoutMS=1000)
        try:
            # The ismaster command is cheap and does not require auth.
            self.database.admin.command('ismaster')
        except PyMongoError:
            self.logger.error(f"Disconnected from MongoDB! Waiting to reconnect now... (uri={self.uri})")
            while True:
                try:
                    self.logger.debug("Attempting to reconnect to MongoDB...")
                    self.database = pymongo.MongoClient(self.uri, serverSelectionTimeoutMS=2000,
                                                        connectTimeoutMS=1000, socketTimeoutMS=1000)
                    self.database.admin.command('ismaster')
                except PyMongoError:
                    self.logger.warning(f"Failed to reconnect to MongoDB! (uri={self.uri})")
                    continue
                self.logger.info("Successfully reconnected to MongoDB!")
                break

    def save(self):
        """
        Save all the data to MongoDB.
        """
        self.logger.info(f"Beginning to save cache of length {len(str(self._cache).encode('utf-8'))} bytes.")
        for flight in self._cache:
            icao = flight[0]
            zone = flight[1]
            level = flight[2]
            data = self._cache[flight]
            data[STORE_INTERNAL][STORE_PACKET_TYPE] = {str(i): data[STORE_INTERNAL][STORE_PACKET_TYPE][i]
                                                       for i in data[STORE_INTERNAL][STORE_PACKET_TYPE]}
            database = self.database.get_database(icao.lower())  # Database is plane ID
            # Truncate the flight start time for use, so it's cleaner
            try:
                collection = database.get_collection(str(int(data[STORE_INTERNAL][STORE_FIRST_PACKET]))
                                                     + "-" + zone + "-" + level)
            except PyMongoError:
                self.connect_to_database()
                collection = database.get_collection(str(int(data[STORE_INTERNAL][STORE_FIRST_PACKET]))
                                                     + "-" + zone + "-" + level)

            for data_type in [STORE_RECV_DATA, STORE_CALC_DATA]:  # Add data to database.
                # This is received and calculated data
                for item in data[data_type]:
                    document = {STORAGE_CATEGORY: data_type,
                                STORAGE_DATA_TYPE: item,
                                STORAGE_DATA: [[datum.time, datum.value] for datum in data[data_type][item]]}
                    try:
                        collection.insert_one(document)
                    except PyMongoError:
                        self.connect_to_database()

            # Add plane information to database. This is done under the STORE_INFO variable
            document = {STORAGE_CATEGORY: STORE_INFO, STORAGE_ZONE: zone, STORAGE_LEVEL: level}
            for info_type in [STORE_INFO, STORE_INTERNAL]:
                document.update({str(i): data[info_type][i] for i in data[info_type]})
            try:
                collection.insert_one(document)
            except PyMongoError:
                self.connect_to_database()

        # Reset cache
        self.logger.info(f"Now done saving {len(self._cache)} eligible flight-levels.")
        self._cache = {}
