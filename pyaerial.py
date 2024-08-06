"""
The main program. This contains the activation of most connections and the mainloop.

(c) 2024 Julian Reder (quantumbagel)
"""
import logging
import time
import pyModeS as pms
import constants
import ruamel.yaml
import threading
from helpers import Datum
from constants import *
import importlib

logging.basicConfig(level=logging.DEBUG)


def load_configuration():
    yaml = ruamel.yaml.YAML()
    with open(CONFIG_FILE) as config:
        data = yaml.load(config)
    logging.info(f"Loaded configuration from {CONFIG_FILE}.")
    return data


configuration = load_configuration()


logging.basicConfig(level=LOGGING_LEVELS[configuration[CONFIG_GENERAL][CONFIG_GENERAL_LOGGING_LEVEL]])

constants.CONFIGURATION = configuration

# Now, import submodules that need configuration to function
import rosetta
import calculations

interface = importlib.import_module(INTERFACES_FOLDER
                                    + "." +
                                    CONFIG_GENERAL_PACKET_METHODS[
                                        configuration[CONFIG_GENERAL][CONFIG_GENERAL_PACKET_METHOD]])


def initiate_generator():
    signal_thread = threading.Thread(target=interface.run, daemon=True)
    signal_thread.start()
    return signal_thread


generator_thread = initiate_generator()

vehicle_categories = {2: {1: "Surface Emergency Vehicle", 3: "Surface Service Vehicle", 4: "Ground Obstruction (4)",
                          5: "Ground Obstruction (5)", 6: "Ground Obstruction (6)", 7: "Ground Obstruction (7)"},
                      3: {1: "Glider/Sailplane", 2: "Lighter-than-air", 3: "Parachutist/Skydiver",
                          4: "Ultralight/Hang-glider/paraglider", 6: "UAV (unmanned aerial vehicle)",
                          7: "Space/transatmospheric vehicle"},
                      4: {1: "Light (<7000kg)", 2: "Medium 1 (7000 to 34000kg)", 3: "Medium 2 (34000 to 136000kg)",
                          4: "High vortex aircraft", 5: "Heavy (>13600kg)",
                          6: "High performance (>5g) and high speed (>740km/h)", 7: "Rotorcraft (helicopter)"}}

main_logger = logging.getLogger("Main")


def classify(msg):
    """
    Classify an ADS-B message
    ASSUMES downlink=17 or 18
    :param msg: the message to classify
    :return: some data ig
    """
    typecode = pms.typecode(msg)
    log = main_logger.getChild("classify")
    if typecode == -1:
        log.debug(f"Invalid ADS-B message, ignoring. {msg}. It is likely to be TC 0, not implemented yet.")
        return
    data = None
    icao = pms.icao(msg)
    typecode_category = "Unknown"
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
        if 9 <= typecode <= 18:
            typecode_category = 3
        else:
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
        data.update({STORE_CALC_DATA: {}})
    else:
        print("wut", typecode, msg)
    return data, typecode_category


def check_should_be_added(packets, new_packet):
    return packets[-1].value != new_packet.value


def check_generator_thread():
    log = main_logger.getChild("check_generator_thread")
    global generator_thread
    if not generator_thread.is_alive():
        log.warning("Generator thread has died! Waiting 3 seconds to restart...")
        time.sleep(3)  # Wait it out
        log.warning("Restarting generator thread...")
        generator_thread = initiate_generator()


def process_messages(msgs):
    global planes
    processed = 0
    for message in msgs:
        try:
            message_data, typecode_cat = classify(message[0])
        except TypeError:
            continue
        processed += 1
        icao = message_data[STORE_INFO][STORE_ICAO]
        if icao not in planes.keys():  # Do we have this plane in our current tracker?
            planes[icao] = message_data  # We don't, so just format the data and insert it
            for item in planes[icao][STORE_RECV_DATA]:
                c_item = planes[icao][STORE_RECV_DATA][item]
                planes[icao][STORE_RECV_DATA][item] = [Datum(c_item, message[1])]
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
            planes[icao].update({STORE_INTERNAL:
                                {STORE_MOST_RECENT_PACKET: message[1], STORE_TOTAL_PACKETS: 1,
                                 STORE_FIRST_PACKET: message[1], STORE_PACKET_TYPE: {typecode_cat: 1}}})
        else:
            internal_data_storage = planes[icao][STORE_INTERNAL]
            internal_data_storage[STORE_MOST_RECENT_PACKET] = message[1]
            internal_data_storage[STORE_TOTAL_PACKETS] += 1
            if typecode_cat in internal_data_storage[STORE_PACKET_TYPE].keys():
                internal_data_storage[STORE_PACKET_TYPE][typecode_cat] += 1
            else:
                internal_data_storage[STORE_PACKET_TYPE][typecode_cat] = 1
    return processed


def calculate():
    for plane in planes:
        calculations.calculate_plane(planes[plane])


def check_for_old_planes(current_time):
    global planes
    old_planes = []
    for plane in planes:
        last_packet_relative_time_ago = current_time - planes[plane][STORE_INTERNAL][STORE_MOST_RECENT_PACKET]
        if last_packet_relative_time_ago > configuration[CONFIG_GENERAL][CONFIG_GENERAL_REMEMBER]:
            old_planes.append(plane)
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


def get_top_planes(current_planes: dict, top: int = None) -> str:
    """
    Return a formatted message containing the top X planes

    :param current_planes: the current planes
    :param top: the top X planes to format
    """
    planes_by_packets = {p: current_planes[p][STORE_INTERNAL][STORE_TOTAL_PACKETS] for p in current_planes.keys()}
    sorted_planes = dict(sorted(planes_by_packets.items(), key=lambda item: item[1], reverse=True))
    message = ""
    if top:
        for current_number, plane in enumerate(sorted_planes):
            if current_number + 1 == top and top != -1:
                break
            message += f"{plane} ({sorted_planes[plane]}), "
    if not message:
        return ""
    if top == -1:
        return f"Top planes: " + message[:-2]
    if top > len(sorted_planes):
        top = len(sorted_planes)
    return f"Top {top} planes: " + message[:-2]


planes = {}
processed_messages = 0
saver = rosetta.MongoSaver(configuration[CONFIG_GENERAL][CONFIG_GENERAL_MONGODB])
top_planes = configuration[CONFIG_GENERAL][CONFIG_GENERAL_TOP_PLANES]
while True:
    start_time = time.time()
    check_generator_thread()
    processed_messages += process_messages(interface.message_queue[:])
    calculate()
    interface.message_queue = []  # Reset the messages
    old = check_for_old_planes(time.time())
    # Print all generated plane data
    main_logger.info(f"Currently tracking {len(planes.keys())} planes. {get_top_planes(planes, top_planes)}")
    process_old_planes(old, saver)
    end_time = time.time()
    delta = 1 / configuration[CONFIG_GENERAL][CONFIG_GENERAL_HERTZ] - (end_time - start_time)
    if delta > 0:
        time.sleep(delta)
