"""
Rosetta is PyAerial's module for filtering and saving to the database.
"""

import logging
import math

import pymongo
from pymongo.errors import NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError, AutoReconnect

import calculations
from constants import *
import helpers
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
                i % int(method.replace(CONFIG_CAT_SAVE_METHOD_DECIMATE, "")) == 0]
    elif method.startswith(CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE):
        arg = tuple([float(i) for i in method
                    .replace(CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE, "")
                    .replace("(", "")
                    .replace(")", "")
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
                time = levels[level][CONFIG_ZONES_LEVELS_TIME]
                minimum_eta = math.inf

                for i, latitude_datum in enumerate(received_information[STORE_LAT]):
                    longitude_datum = calculations.get_latest(STORE_RECV_DATA, STORE_LONG, plane,
                                                              latitude_datum.time)
                    latest_direction = calculations.get_latest(STORE_CALC_DATA, STORE_HEADING, plane,
                                                               latitude_datum.time)
                    latest_speed = calculations.get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane,
                                                           latitude_datum.time)
                    eta = calculations.time_to_enter_geofence([latitude_datum.value, longitude_datum.value],
                                                              latest_direction.value,
                                                              latest_speed.value,
                                                              CONFIGURATION[CONFIG_ZONES][zone][
                                                                  CONFIG_ZONES_COORDINATES],
                                                              time)
                    if eta < minimum_eta:
                        minimum_eta = eta
                if minimum_eta <= time:
                    filtered_received_information = filter_packets(received_information,
                                                                   CONFIGURATION[CONFIG_CATEGORIES][category][
                                                                       CONFIG_CAT_SAVE]
                                                                   [CONFIG_CAT_SAVE_TELEMETRY_METHOD])
                    filtered_calculated_information = filter_packets(calculated_information,
                                                                     CONFIGURATION[CONFIG_CATEGORIES][category][
                                                                         CONFIG_CAT_SAVE]
                                                                     [CONFIG_CAT_SAVE_CALCULATED_METHOD])

                    self.add_plane_to_cache(plane[STORE_INFO][STORE_ICAO], zone, level,
                                            {STORE_CALC_DATA: filtered_calculated_information,
                                             STORE_RECV_DATA: filtered_received_information,
                                             STORE_INTERNAL: internal_information, STORE_INFO: information})


class PrintSaver(Saver):
    def __init__(self):
        super().__init__(log_name="PrintSaver")

    def save(self):
        self.logger.info(f"SAVING: {self._cache}")
        self._cache = {}


class MongoSaver(Saver):
    def __init__(self, uri):
        super().__init__(log_name="MongoSaver")
        self.database: pymongo.MongoClient = None
        self.uri = uri
        self.connect_to_database()

    def connect_to_database(self):
        self.database = pymongo.MongoClient(self.uri, serverSelectionTimeoutMS=2000,
                                            connectTimeoutMS=1000, socketTimeoutMS=1000)
        try:
            # The ismaster command is cheap and does not require auth.
            self.database.admin.command('ismaster')
        except (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError, AutoReconnect):
            self.logger.error(f"Disconnected from MongoDB! Waiting to reconnect now... (uri={self.uri})")
            while True:
                try:
                    self.logger.debug("Attempting to reconnect to MongoDB...")
                    self.database = pymongo.MongoClient(self.uri, serverSelectionTimeoutMS=2000,
                                                        connectTimeoutMS=1000, socketTimeoutMS=1000)
                    self.database.admin.command('ismaster')
                except (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError, AutoReconnect):
                    self.logger.warning(f"Failed to reconnect to MongoDB! (uri={self.uri})")
                    continue
                self.logger.info("Successfully reconnected to MongoDB!")
                break

    def save(self):
        """
        Save all the data to MongoDB.
        """
        self.logger.info(f"now saving: {self._cache}")
        for flight in self._cache:
            icao = flight[0]
            zone = flight[1]
            level = flight[2]
            data = self._cache[flight]
            data[STORE_INTERNAL][STORE_PACKET_TYPE] = {str(i): data[STORE_INTERNAL][STORE_PACKET_TYPE][i]
                                                       for i in data[STORE_INTERNAL][STORE_PACKET_TYPE]}
            database = self.database.get_database(icao.lower())  # Database is plane ID
            # Truncate the flight start time for use, so it's cleaner
            collection = database.get_collection(str(int(data[STORE_INTERNAL][STORE_FIRST_PACKET])))

            for data_type in [STORE_RECV_DATA, STORE_CALC_DATA]:  # Add data to database.
                # This is received and calculated data
                for item in data[data_type]:
                    document = {STORAGE_CATEGORY: data_type,
                                STORAGE_DATA_TYPE: item,
                                STORAGE_DATA: [[datum.time, datum.value] for datum in data[data_type][item]]}
                    collection.insert_one(document)

            # Add plane information to database. This is done under the STORE_INFO variable
            document = {STORAGE_CATEGORY: STORE_INFO, STORAGE_ZONE: zone, STORAGE_LEVEL: level}
            for info_type in [STORE_INFO, STORE_INTERNAL]:
                document.update({str(i): data[info_type][i] for i in data[info_type]})
            collection.insert_one(document)

        # Reset cache
        self.logger.info(f"done saving {len(self._cache)} planes.")
        self._cache = {}
