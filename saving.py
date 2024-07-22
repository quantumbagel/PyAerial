import pymongo
from pymongo.errors import NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError, AutoReconnect
import logging
import calculations
from constants import *
import ruamel.yaml
import math
import helpers
import rosetta

database = None
log = logging.getLogger("Database")


config = ruamel.yaml.YAML().load(open(CONFIG_FILE))


def connect_to_database(uri):
    global database
    database = pymongo.MongoClient(uri, serverSelectionTimeoutMS=2000,
                                   connectTimeoutMS=1000, socketTimeoutMS=1000)
    try:
        # The ismaster command is cheap and does not require auth.
        database.admin.command('ismaster')
    except (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError, AutoReconnect):
        log.error(f"Disconnected from MongoDB! Waiting to reconnect now... (uri={uri})")
        while True:
            try:
                log.debug("Attempting to reconnect to MongoDB...")
                database = pymongo.MongoClient(uri, serverSelectionTimeoutMS=2000,
                                               connectTimeoutMS=1000, socketTimeoutMS=1000)
                database.admin.command('ismaster')
            except (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError, AutoReconnect):
                log.warning(f"Failed to reconnect to MongoDB! (uri={uri})")
                continue
            log.info("Successfully reconnected to MongoDB!")
            break


def filter_packets(packets, method=CONFIG_CAT_SAVE_METHOD_ALL):
    if method == CONFIG_CAT_SAVE_METHOD_ALL:
        return packets
    elif method.startswith(CONFIG_CAT_SAVE_METHOD_DECIMATE):
        return [p for i, p in enumerate(packets) if i % int(method.replace(CONFIG_CAT_SAVE_METHOD_DECIMATE, "")) == 0]
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


def add_flight_to_database(plane, saver: rosetta.Saver):
    saver = rosetta.PrintSaver()
    for item in [STORE_RECV_DATA, STORE_CALC_DATA]:
        for datum in plane[item]:
            plane[item][datum] = [helpers.Datum(pf[0], pf[1]) for pf in plane[item][datum]]
    information = plane[STORE_INFO]
    calculated_information = plane[STORE_CALC_DATA]
    received_information = plane[STORE_RECV_DATA]
    internal_information = plane[STORE_INTERNAL]

    for zone in config[CONFIG_ZONES]:
        levels = config[CONFIG_ZONES][zone][CONFIG_ZONES_LEVELS]
        for level in levels:
            category = levels[level][CONFIG_ZONES_LEVELS_CATEGORY]
            time = levels[level][CONFIG_ZONES_LEVELS_TIME]
            minimum_eta = math.inf
            for i, latitude_datum in enumerate(received_information[STORE_LAT]):
                longitude_datum = received_information[STORE_LONG][i]
                latest_direction = calculations.get_latest(STORE_CALC_DATA, STORE_HEADING, plane, latitude_datum.time)
                latest_speed = calculations.get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, latitude_datum.time)
                eta = calculations.time_to_enter_geofence([latitude_datum.value, longitude_datum.value],
                                                          latest_direction.value,
                                                          latest_speed.value,
                                                          config[CONFIG_ZONES][zone][CONFIG_ZONES_COORDINATES], time)
                if eta < minimum_eta:
                    minimum_eta = eta
            if minimum_eta <= time:
                filtered_received_information = filter_packets(received_information,
                                                               config[CONFIG_CATEGORIES][category][CONFIG_CAT_SAVE]
                                                               [CONFIG_CAT_SAVE_TELEMETRY_METHOD])
                filtered_calculated_information = filter_packets(calculated_information,
                                                                 config[CONFIG_CATEGORIES][category][CONFIG_CAT_SAVE]
                                                                 [CONFIG_CAT_SAVE_CALCULATED_METHOD])

                saver.add_plane_to_cache(plane[STORE_INFO][STORE_ICAO],
                                         {STORE_CALC_DATA: filtered_calculated_information,
                                          STORE_RECV_DATA: filtered_received_information,
                                          STORE_INTERNAL: internal_information, STORE_INFO: information})
        saver.save()
