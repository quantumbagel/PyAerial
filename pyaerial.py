"""
The main program. This contains the activation of most connections and the mainloop.

(c) 2024 Julian Reder (quantumbagel)
"""
import logging
import sys
import time
import pyModeS as pms
import constants
import ruamel.yaml
import threading
from helpers import Datum
from constants import *
import importlib


def load_configuration() -> dict:
    """
    Load the configuration from config.yaml (or constants.CONFIG_FILE).
    :return: the data from the configuration file
    Will crash if file does not exist. This is intentional
    """
    yaml = ruamel.yaml.YAML()
    with open(CONFIG_FILE) as config:
        data = yaml.load(config)
    return data


configuration = load_configuration()  # Load the configuration

logging.basicConfig(level=LOGGING_LEVELS[configuration[CONFIG_GENERAL][CONFIG_GENERAL_LOGGING_LEVEL]])

constants.CONFIGURATION = configuration  # Share configuration with constants.py

# Now, import submodules that need use constants.py's configuration variable to function
import rosetta
import calculations

interface = importlib.import_module(INTERFACES_FOLDER  # Import the interface defined in the configuration.
                                    + "." +
                                    CONFIG_GENERAL_PACKET_METHODS[
                                        configuration[CONFIG_GENERAL][CONFIG_GENERAL_PACKET_METHOD]])


def initiate_generator() -> threading.Thread:
    """
    Initiate the generator based on the interface module.
    :return: the generator thread (threading.Thread) after it has been started.
    """
    signal_thread = threading.Thread(target=interface.run, daemon=True)  # Daemon so main thread can take it down
    signal_thread.start()
    return signal_thread


generator_thread = initiate_generator()  # Start the signal generator


main_logger = logging.getLogger("Main")  # Main program logger

logging.info("Hi! PyAerial has successfully loaded all modules and is about to start.")


def classify(msg) -> (dict, int):
    """
    Classify an ADS-B message
    ASSUMES downlink=17 or 18
    :param msg: the message to classify
    :return: some data ig
    """
    typecode = pms.typecode(msg)  # Get the typecode of the message
    log = main_logger.getChild("classify")  # Logger
    if typecode == -1:  # Message that pms can't handle yet, or message that dump1090 can that pms can't :/
        # This way we know that the plane is "still around"
        data = {STORE_INFO: {STORE_ICAO: pms.icao(msg)}, STORE_RECV_DATA: {}, STORE_CALC_DATA: {}}
        return data, 0
    data = None
    icao = pms.icao(msg)  # ICAO of message (every message shares this)
    if len(icao) != 6 or icao == "000000":  # Invalid ICAO length or zero-icao
        return
    typecode_category = -1

    if 1 <= typecode <= 4:  # Aircraft identification
        ca = pms.adsb.category(msg)
        data = {STORE_INFO: {STORE_ICAO: icao, STORE_CALLSIGN: pms.adsb.callsign(msg).replace("_", ""),
                             STORE_PLANE_CATEGORY: [typecode, ca]}, STORE_RECV_DATA: {}}
        typecode_category = 1

    elif 5 <= typecode <= 8:  # Surface position
        lat, lon = pms.adsb.position_with_ref(msg, configuration[CONFIG_HOME][CONFIG_HOME_LATITUDE],
                                              configuration[CONFIG_HOME][CONFIG_HOME_LONGITUDE])
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(msg, source=True)
        data = {STORE_INFO: {STORE_ICAO: icao},
                STORE_RECV_DATA: {STORE_LAT: lat, STORE_LONG: lon, STORE_HORIZ_SPEED: speed * 1.852,
                                  STORE_HEADING: angle, STORE_VERT_SPEED: vert_rate * 0.00508}}
        typecode_category = 2

    elif 9 <= typecode <= 18 or 20 <= typecode <= 22:  # Airborne position (barometric alt/GNSS alt)
        lat, lon = pms.adsb.position_with_ref(msg, configuration[CONFIG_HOME][CONFIG_HOME_LATITUDE],
                                              configuration[CONFIG_HOME][CONFIG_HOME_LONGITUDE])
        alt = pms.adsb.altitude(msg) * 0.3048  # convert feet to meters
        data = {STORE_INFO: {STORE_ICAO: icao}, STORE_RECV_DATA: {STORE_LAT: lat, STORE_LONG: lon, STORE_ALT: alt}}
        if 9 <= typecode <= 18:  # 9-18 is barometric
            typecode_category = 3
        else:  # Others are GNSS
            typecode_category = 4

    elif typecode == 19:  # Airborne velocities
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(msg, source=True)
        data = {STORE_INFO: {STORE_ICAO: icao},
                STORE_RECV_DATA: {STORE_HORIZ_SPEED: speed * 1.852, STORE_HEADING: angle,
                                  STORE_VERT_SPEED: vert_rate * 0.00508}}
        typecode_category = 5

    elif typecode == 28:  # Aircraft status
        return
    elif typecode == 29:  # Target state and status information
        return
    elif typecode == 31:  # Aircraft operation status
        return  # Not going to implement this type of message
    if data is not None:
        log.debug(f"Collected ADS-B message from typecode {typecode}: {data}")
        data.update({STORE_CALC_DATA: {}})  # Ensure we have calculated data stub
    else:
        log.error(f"Received confusing typecode {typecode} (msg={msg})")
    return data, typecode_category


def check_should_be_added(packets, new_packet):
    """
    A really simple function to determine if a new packet shares the value of the one directly preceding it.
    :param packets: ALL of the packets
    :param new_packet: the new packet we are planning to add
    """
    return packets[-1].value != new_packet.value


def check_generator_thread() -> None:
    """
    Make sure our generator is still running. Will call initiate_generator() if not.
    """
    log = main_logger.getChild("check_generator_thread")
    global generator_thread
    if not generator_thread.is_alive():
        log.warning("Generator thread has died! Waiting 3 seconds to restart...")
        time.sleep(3)  # Wait it out
        log.warning("Restarting generator thread...")
        generator_thread = initiate_generator()


def process_messages(msgs) -> int:
    """
    Process every raw ADS-B message. Updates the planes variable.
    :return: the messages processed
    """
    global planes
    processed = 0
    for message in msgs:
        try:
            message_data, typecode_cat = classify(message[0])  # Get the data out from the message
        except TypeError:  # If it failed somehow, just continue
            continue
        processed += 1
        icao = message_data[STORE_INFO][STORE_ICAO]  # Get the ICAO
        if icao not in planes.keys():  # Do we have this plane in our current tracker?
            planes[icao] = message_data  # We don't, so just format the data and insert it
            for item in planes[icao][STORE_RECV_DATA]:
                c_item = planes[icao][STORE_RECV_DATA][item]
                planes[icao][STORE_RECV_DATA][item] = [Datum(c_item, message[1])]  # Save the received data as Datum
        else:  # We do, so find and replace (or update) data
            current_info = planes[icao][STORE_INFO]  # Current plane info
            my_info = message_data[STORE_INFO]
            for item in my_info.keys():  # Find and replace/update data
                if item not in current_info.keys():
                    current_info.update({item: my_info[item]})
                else:
                    current_info[item] = my_info[item]
            planes[icao][STORE_INFO] = current_info
            current_data = planes[icao][STORE_RECV_DATA]
            # Similar thing here, but using lists
            for datum in message_data[STORE_RECV_DATA].keys():
                new_packet = Datum(message_data[STORE_RECV_DATA][datum], message[1])
                if datum not in current_data.keys():
                    current_data.update({datum: [new_packet]})
                else:
                    if check_should_be_added(current_data[datum], new_packet):
                        current_data[datum].append(new_packet)

        if STORE_INTERNAL not in planes[icao].keys():  # Update internal metrics
            # Base internal information for when we don't have it
            planes[icao].update({STORE_INTERNAL:
                                {STORE_MOST_RECENT_PACKET: message[1], STORE_TOTAL_PACKETS: 1,
                                 STORE_FIRST_PACKET: message[1], STORE_PACKET_TYPE: {typecode_cat: 1}}})  #
        else:  # Update internal information (we already have it)
            internal_data_storage = planes[icao][STORE_INTERNAL]
            internal_data_storage[STORE_MOST_RECENT_PACKET] = message[1]
            internal_data_storage[STORE_TOTAL_PACKETS] += 1
            if typecode_cat in internal_data_storage[STORE_PACKET_TYPE].keys():
                internal_data_storage[STORE_PACKET_TYPE][typecode_cat] += 1
            else:
                internal_data_storage[STORE_PACKET_TYPE][typecode_cat] = 1
    return processed


def calculate() -> None:
    """
    Run calculate_plane on each plane.
    :return: None
    """
    for plane in planes:
        calculations.calculate_plane(planes[plane])


def check_for_old_planes(current_time) -> list:
    """
    Check if planes are old (haven't received information in a while).
    """
    global planes
    old_planes = []
    for plane in planes:
        last_packet_relative_time_ago = current_time - planes[plane][STORE_INTERNAL][STORE_MOST_RECENT_PACKET]
        if last_packet_relative_time_ago > configuration[CONFIG_GENERAL][CONFIG_GENERAL_REMEMBER]:  # Too old?
            old_planes.append(plane)  # Yes
    return old_planes


def process_old_planes(old_planes: list, defined_saver: rosetta.Saver) -> None:
    """
    Cache and save old planes using the defined Saver. Note that the Saver must already be
    initialized for this method to work.

    :param old_planes: the planes that are old
    :param defined_saver: the Saver to save the old planes
    """
    log = main_logger.getChild("process_old_planes")
    for plane in old_planes:
        log.debug(f"Old plane processed! {len(planes) - 1} {planes[plane]}")
        defined_saver.cache_flight(planes[plane])
        del planes[plane]
    if len(old_planes):  # We actually removed planes
        defined_saver.save()


def get_top_planes(current_planes: dict, top: int = None, advanced: bool = False) -> str:
    """
    Return a formatted message containing the top X planes

    :param current_planes: the current planes
    :param top: the top X planes to format
    """
    # a dictionary of {plane_id: total_packets}
    planes_by_packets = {p: current_planes[p][STORE_INTERNAL][STORE_TOTAL_PACKETS] for p in current_planes.keys()}
    # sorting these planes by their packets
    sorted_planes = dict(sorted(planes_by_packets.items(), key=lambda item: item[1], reverse=True))
    message = ""
    if top:  # Are we even to display the top X planes (i.e. is top not 0)?
        for current_number, plane in enumerate(sorted_planes):
            if current_number + 1 == top and top != -1:  # Are we done?
                break
            if not advanced:
                message += f"{plane} ({sorted_planes[plane]}), "  # Add the plane to the message
            else:
                if (STORE_CALLSIGN in current_planes[plane][STORE_INFO].keys() and
                        current_planes[plane][STORE_INFO][STORE_CALLSIGN]):
                    message += (f"{plane}/{current_planes[plane][STORE_INFO][STORE_CALLSIGN]} ({sorted_planes[plane]},"
                                f" {current_planes[plane][STORE_INTERNAL][STORE_PACKET_TYPE]}), ")
                else:
                    message += (f"{plane}, ({sorted_planes[plane]},"
                                f" {current_planes[plane][STORE_INTERNAL][STORE_PACKET_TYPE]}), ")
    if not message:
        return ""
    if top == -1:  # Hold on to every plane
        return message[:-2]
    if top > len(sorted_planes):  # Say "Top 4 planes: {4 planes} instead of Top 5 planes: {4 planes}
        top = len(sorted_planes)
    return f"Top {top}: " + message[:-2]


planes = {}  # Plane data
processed_messages = 0
saver = rosetta.MongoSaver(configuration[CONFIG_GENERAL][CONFIG_GENERAL_MONGODB])
top_planes = configuration[CONFIG_GENERAL][CONFIG_GENERAL_TOP_PLANES]
while True:
    start_time = time.time()
    status = ""  # Check if we are receiving new information, so we can log that.
    if not generator_thread.is_alive():
        status = "[PAUSED] "
    check_generator_thread()
    processed_messages += process_messages(interface.message_queue[:])
    calculate()
    interface.message_queue = []  # Reset the messages
    old = check_for_old_planes(time.time())

    # Print all generated plane data
    main_logger.info(f"{status}Tracking {len(planes.keys())} planes."
                     f""" {get_top_planes(planes,
                                          top_planes,
                                          configuration[CONFIG_GENERAL][CONFIG_GENERAL_ADVANCED_STATUS])}""")
    process_old_planes(old, saver)
    end_time = time.time()
    delta = 1 / configuration[CONFIG_GENERAL][CONFIG_GENERAL_HERTZ] - (end_time - start_time)
    if delta > 0:
        try:
            time.sleep(delta)  # Sleep the delta away
        except KeyboardInterrupt:
            main_logger.critical("Now quitting (keyboard interrupt)")
            sys.exit(0)
