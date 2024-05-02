import time
import pyModeS as pms
import calculations
import signal_generator
import ruamel.yaml
import threading
from constants import *


def initiate_signal_generator():
    signal_thread = threading.Thread(target=signal_generator.run, daemon=True)
    signal_thread.start()
    return signal_thread


# https://stackoverflow.com/questions/33311616/find-coordinate-of-the-closest-point-on-polygon-in-shapely
def load_configuration():
    yaml = ruamel.yaml.YAML()
    data = yaml.load(open('config.yaml'))
    home = data['home']
    home_lat = home['latitude']
    home_lon = home['longitude']
    general = {'home': [home_lat, home_lon]}

    return general, data['zones'], data['categories']


configuration, zones, categories = load_configuration()

calculations.share_configuration_context(configuration, zones, categories)


generator_thread = initiate_signal_generator()

vehicle_categories = {2: {1: "Surface Emergency Vehicle", 3: "Surface Service Vehicle", 4: "Ground Obstruction (4)",
                          5: "Ground Obstruction (5)", 6: "Ground Obstruction (6)", 7: "Ground Obstruction (7)"},
                      3: {1: "Glider/Sailplane", 2: "Lighter-than-air", 3: "Parachutist/Skydiver",
                          4: "Ultralight/Hang-glider/paraglider", 6: "UAV (unmanned aerial vehicle)",
                          7: "Space/transatmospheric vehicle"},
                      4: {1: "Light (<7000kg)", 2: "Medium 1 (7000 to 34000kg)", 3: "Medium 2 (34000 to 136000kg)",
                          4: "High vortex aircraft", 5: "Heavy (>13600kg)",
                          6: "High performance (>5g) and high speed (>740km/h)", 7: "Rotorcraft (helicopter)"}}


def classify(msg):
    """
    Classify an ADS-B message
    ASSUMES downlink=17 or 18
    :param msg: the message to classify
    :return: some data ig
    """
    typecode = pms.typecode(msg)
    if typecode == -1:
        print(f"Invalid ADS-B message, ignoring. {msg}")
        return
    data = None
    icao = pms.icao(msg)
    typecode_category = "Unknown"
    if 1 <= typecode <= 4:  # Aircraft identification
        ca = pms.adsb.category(msg)
        data = {STORE_INFO: {"icao": icao, "callsign": pms.adsb.callsign(msg).replace("_", ""),
                             "category": [typecode, ca]}, STORE_RECV_DATA: {}}
        typecode_category = 1

    elif 5 <= typecode <= 8:  # Surface position
        lat, lon = pms.adsb.position_with_ref(msg, configuration['home'][0], configuration['home'][1])
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(msg, source=True)
        data = {STORE_INFO: {"icao": icao},
                STORE_RECV_DATA: {"latitude": lat, "longitude": lon, "horizontal_speed": speed * 1.852,
                                  "direction": angle, "vertical_speed": vert_rate * 0.018288}}
        typecode_category = 2

    elif 9 <= typecode <= 18 or 20 <= typecode <= 22:  # Airbone position (baro alt/GNSS alt)
        lat, lon = pms.adsb.position_with_ref(msg, configuration['home'][0], configuration['home'][1])
        alt = pms.adsb.altitude(msg)
        data = {STORE_INFO: {"icao": icao}, STORE_RECV_DATA: {"latitude": lat, "longitude": lon, "altitude": alt}}
        if 9 <= typecode <= 18:
            typecode_category = 3
        else:
            typecode_category = 4

    elif typecode == 19:  # Airbone velocities
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(msg, source=True)
        data = {STORE_INFO: {"icao": icao},
                STORE_RECV_DATA: {"horizontal_speed": speed * 1.852, "direction": angle,
                                  "vertical_speed": vert_rate * 0.018288}}
        typecode_category = 5

    elif typecode == 28:  # Aircraft status
        pass
    elif typecode == 29:  # Target state and status information
        pass
    elif typecode == 31:  # Aircraft operation status
        pass  # Not going to implement this type of message
    if data is not None:
        data.update({STORE_CALC_DATA: {}})
    return data, typecode_category


def check_should_be_added(packets, new_packet):
    return packets[-1][0] != new_packet[0]


def check_generator_thread():
    global generator_thread
    if not generator_thread.is_alive():
        print("Generator thread has died! Waiting 3 seconds to restart...")
        time.sleep(3)  # Wait it out
        print("Restarting generator thread...")
        generator_thread = initiate_signal_generator()

def process_messages(msgs):
    global planes
    processed = 0
    for message in msgs:
        message_data, typecode_cat = classify(message[0])
        if message_data is None:  # Not implemented yet :(
            continue
        processed += 1
        icao = message_data[STORE_INFO]["icao"]
        if icao not in planes.keys():  # Do we have this plane in our current tracker?
            planes[icao] = message_data  # We don't, so just format the data and insert it
            for item in planes[icao][STORE_RECV_DATA]:
                c_item = planes[icao][STORE_RECV_DATA][item]
                planes[icao][STORE_RECV_DATA][item] = [[c_item, message[1]]]
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
                new_packet = [message_data[STORE_RECV_DATA][datum], message[1]]
                if datum not in current_data.keys():
                    current_data.update({datum: [new_packet]})
                else:
                    if check_should_be_added(current_data[datum], new_packet):
                        current_data[datum].append(new_packet)

        if "internal" not in planes[icao].keys():  # Update internal metrics
            planes[icao].update({"internal":
                                     {"last_update": message[1], "packets": 1,
                                      "first_packet": message[1], "packet_type": {typecode_cat: 1}}})
        else:
            internal_data_storage = planes[icao]["internal"]
            internal_data_storage["last_update"] = message[1]
            internal_data_storage["packets"] += 1
            if typecode_cat in internal_data_storage["packet_type"].keys():
                internal_data_storage["packet_type"][typecode_cat] += 1
            else:
                internal_data_storage["packet_type"][typecode_cat] = 1
    return processed


def calculate():
    for plane in planes:
        calculations.calculate_plane(planes[plane])


def check_for_old_planes(current_time):
    global planes
    old_planes = []
    for plane in planes:
        last_packet_relative_time_ago = current_time - planes[plane]['internal']['last_update']
        if last_packet_relative_time_ago > REMEMBER_PLANES:
            old_planes.append(plane)
    return old_planes


def process_old_planes(old_planes):
    for plane in old_planes:
        print("Old plane processed! (newlen, removed plane)=", len(planes) - 1, planes[plane])
        del planes[plane]


planes = {}
processed_messages = 0
while True:
    check_generator_thread()
    processed_messages += process_messages(signal_generator.message_queue[:])
    calculate()
    signal_generator.message_queue = []  # Reset the messsages
    old = check_for_old_planes(time.time())
    print(len(planes.keys()), planes)  # Print all generated plane data
    process_old_planes(old)
    time.sleep(0.5)
