import logging
import math
import threading
import time
import pyModeS as pms
import requests

import signal_generator
import ruamel.yaml
import geopy.distance
from shapely.geometry import Point, Polygon

STORE_INFO = "info"
STORE_RECV_DATA = "received_data"
STORE_CALC_DATA = "calculated_data"
MAX_SPEED_CALC_BACK_PACKET = 10
BACKDATE_HORIZONTAL_SPEED = 10
REMEMBER_PLANES = 60
POINT_ACCURACY_THRESHOLD_DEG = 10


def initate_signal_generator():
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

generator_thread = initate_signal_generator()

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
        print(speed, angle, vert_rate, speed_type, angle_source, vert_rate_source)
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
        generator_thread = initate_signal_generator()


def calculate_heading(previous, current):
    """
    Calculate the direction of the heading from the lat long pair previous to the lat long pair current.
    :param previous: The previous position (lat/long)
    :param current: The current position (lat/long)
    :return: The heading (degrees)
    """

    pi_c = math.pi / 180
    first_lat = previous[0] * pi_c
    first_lon = previous[1] * pi_c
    second_lat = current[0] * pi_c
    second_lon = current[1] * pi_c
    y = math.sin(second_lon - first_lon) * math.cos(second_lat)
    x = (math.cos(first_lat) * math.sin(second_lat)) - (
            math.sin(first_lat) * math.cos(second_lat) * math.cos(second_lon - first_lon))
    heading_rads = math.atan2(y, x)
    heading_degs = ((heading_rads * 180 / math.pi) + 360) % 360
    return heading_degs


def calculate_speed(previous, current, previous_time, current_time):
    dist_xz = geopy.distance.geodesic(previous, current).m
    elapsed_time = current_time - previous_time
    speed = dist_xz / elapsed_time * 3.6  # m/s to km/h
    return speed


def patch_append(plane, category, message_type, message):
    if message_type in planes[plane][category].keys():
        planes[plane][category][message_type].append(message)
        return True
    else:
        planes[plane][category][message_type] = [message]
        return True
    # return False


def process_messages(msgs):
    global planes
    processed = 0
    for message in msgs:
        message_data, typecode_cat = classify(message[0])
        if message_data is None:  # Not implemented yet :(
            continue
        processed += 1
        if message_data[STORE_INFO]["icao"] not in planes.keys():  # Do we have this plane in our current tracker?
            planes[message_data[STORE_INFO]["icao"]] = message_data  # We don't, so just format the data and insert it
            for item in planes[message_data[STORE_INFO]["icao"]][STORE_RECV_DATA]:
                c_item = planes[message_data[STORE_INFO]["icao"]][STORE_RECV_DATA][item]
                planes[message_data[STORE_INFO]["icao"]][STORE_RECV_DATA][item] = [[c_item, message[1]]]
        else:  # We do, so find and replace (or update) data
            current_info = planes[message_data[STORE_INFO]["icao"]][STORE_INFO]  # Current plane info
            my_info = message_data[STORE_INFO]
            for item in my_info.keys():  # Find and replace/update data
                if item not in current_info.keys():
                    current_info.update({item: my_info[item]})
                else:
                    current_info[item] = my_info[item]
            planes[message_data[STORE_INFO]["icao"]][STORE_INFO] = current_info
            current_data = planes[message_data[STORE_INFO]["icao"]][STORE_RECV_DATA]
            # Similar thing here, but using lists
            for datum in message_data[STORE_RECV_DATA].keys():
                new_packet = [message_data[STORE_RECV_DATA][datum], message[1]]
                if datum not in current_data.keys():
                    current_data.update({datum: [new_packet]})
                else:
                    if check_should_be_added(current_data[datum], new_packet):
                        current_data[datum].append(new_packet)

        if "internal" not in planes[message_data[STORE_INFO]["icao"]].keys():  # Update internal metrics
            planes[message_data[STORE_INFO]["icao"]].update({"internal": {"last_update": message[1], "packets": 1,
                                                                          "first_packet": message[1], "packet_type":
                                                                              {typecode_cat: 1}}})
        else:
            internal_data_storage = planes[message_data[STORE_INFO]["icao"]]["internal"]
            internal_data_storage["last_update"] = message[1]
            internal_data_storage["packets"] += 1
            if typecode_cat in internal_data_storage["packet_type"].keys():
                internal_data_storage["packet_type"][typecode_cat] += 1
            else:
                internal_data_storage["packet_type"][typecode_cat] = 1
    return processed


def execute_method(method="print", method_arguments=None, payload=None):
    if method == "print":
        icao = method_arguments["icao"]
        tag = method_arguments["callsign"]
        message_type = method_arguments["type"]
        print_me = {"icao": icao, "callsign": tag, "type": message_type, "payload": payload}
        l = logging.getLogger(f"{message_type}")
        l.error(print_me)


def calculate():
    for plane in planes:
        if "latitude" not in planes[plane][STORE_RECV_DATA].keys():  # Haven't yet received latitude/longitude packet
            continue
        latitude_data = planes[plane][STORE_RECV_DATA]["latitude"]
        longitude_data = planes[plane][STORE_RECV_DATA]["longitude"]
        if len(latitude_data) == 1:  # Need at least two lat/long pairs
            continue
        else:
            if len(latitude_data) < MAX_SPEED_CALC_BACK_PACKET:  # If we don't have at least the max calc back packets,
                # set the previous to the first packet
                previous_lat = latitude_data[0]
                previous_lon = longitude_data[0]

            else:
                old_packet = len(latitude_data) - MAX_SPEED_CALC_BACK_PACKET  # Find the indice of the old packet
                previous_lat = latitude_data[old_packet]
                previous_lon = longitude_data[old_packet]
            previous = [previous_lat[0], previous_lon[0]]  # Previous lat/long
            previous_time = previous_lat[1]
            current_lat = latitude_data[-1]
            current_lon = longitude_data[-1]
            current = [current_lat[0], current_lon[0]]  # Current lat/long
            current_time = current_lat[1]
            speed = calculate_speed(previous, current, previous_time, current_time)  # Calculate speed
            heading = calculate_heading(previous, current)  # Calculate heading
            if "horizontal_speed" not in planes[plane][STORE_RECV_DATA].keys():
                # If we haven't received a horiz_speed packet, don't even consider using it
                final_speed = speed
                speed_time = current_time
            else:
                horiz_plane_speed = planes[plane][STORE_RECV_DATA]["horizontal_speed"][-1]
                # Otherwise, get the time of the newest one
                time_ago = current_time - horiz_plane_speed[1]
                if time_ago < MAX_SPEED_CALC_BACK_PACKET:  # Is it new enough to be relevant?
                    final_speed = horiz_plane_speed[0]  # Use it
                    speed_time = horiz_plane_speed[1]
                else:
                    final_speed = speed  # Not relevant, use computed speed
                    speed_time = current_time

            # Add speed to data
            patch_append(plane, STORE_CALC_DATA, "horizontal_speed", [final_speed, speed_time])

            if "heading" not in planes[plane][STORE_RECV_DATA].keys():  # If we don't have heading data
                final_heading = heading  # Use computed heading
            else:
                heading_data = planes[plane][STORE_RECV_DATA]["heading"][-1]  # Get newest heading data
                time_ago = current_time - heading_data[1]
                if time_ago < MAX_SPEED_CALC_BACK_PACKET:  # Is it new enough?
                    final_heading = heading_data[0]  # Use it
                else:
                    final_heading = heading  # Not relevant
            patch_append(plane, STORE_CALC_DATA, "heading", [final_heading, speed_time])

            geofence_etas = {}
            current_warn_category = None
            current_alert_category = None
            print(list(zones.values())[0], current_warn_category, current_alert_category)
            send_warning = False
            send_alert = False
            for geofence_name in zones:
                geofence = zones[geofence_name]
                eta = time_to_enter_geofence(current, final_heading, final_speed, geofence["coordinates"],
                                             geofence["warn_time"])
                geofence_etas[geofence_name] = eta
                if 0 < eta < math.inf:
                    send_warning = True
                if 0 == eta:
                    send_alert = True
                if eta != math.inf and (current_warn_category is None or current_alert_category is None):
                    print(eta, geofence)
                    current_warn_category = geofence["warn_category"]
                    current_alert_category = geofence["alert_category"]
                if 0 < eta < math.inf and categories[geofence["warn_category"]]["priority"] < \
                        categories[current_warn_category]["priority"]:
                    # We are not in the zone YET, but we should sound a warning
                    current_warn_category = geofence["warn_category"]
                if 0 == eta and categories[geofence["alert_category"]]["priority"] < \
                        categories[current_alert_category]["priority"]:
                    # We are in the zone
                    current_alert_category = geofence["alert_category"]

            print("Calert, Cwarning", current_warn_category, current_alert_category)
            print("Geofence ETAs:", geofence_etas)  # TODO: Add actual use of the geofence ETAs
            try:
                callsign = planes[plane]['info']['callsign']
            except KeyError:
                callsign = get_callsign(plane)  # Callsign might not always exist
                if callsign is not None:  # if we got it
                    planes[plane]['info']['callsign'] = callsign  # save it

            if send_alert:
                reason = {"zones": geofence_etas, "category": current_alert_category}
                alert_method_arguments = {"type": "alert", "icao": plane, "callsign": callsign}
                payload = {"reason": reason}  # TODO: add latest lat/long/alt
                print(alert_method_arguments)
                execute_method(categories[current_alert_category]["send_alert"]["method"], alert_method_arguments,
                               payload=payload)
            elif send_warning:
                reason = {"zones": geofence_etas, "category": current_alert_category}
                warn_method_arguments = {"type": "warning", "icao": plane, "callsign": callsign, "reason": reason}
                payload = {"reason": reason}  # TODO: add latest lat/long/alt
                print(warn_method_arguments)
                execute_method(categories[current_alert_category]["send_warning"]["method"], warn_method_arguments,
                               payload=payload)


def get_callsign(icao):
    resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao}", timeout=1)
    json = resp.json()
    if "Registration" in json.keys():
        return json["Registration"]
    else:
        return None


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
        print(len(planes) - 1, planes[plane])
        del planes[plane]


def time_to_enter_geofence(plane_position, heading, speed, geofence_coordinates, max_time):
    # Create a shapely Polygon object from the geofence coordinates
    geofence_polygon = Polygon(geofence_coordinates)

    # Check if the current position is inside the geofence
    if geofence_polygon.contains(Point(plane_position)):
        return 0  # The plane is already inside the geofence

    verified = False
    for point in geofence_polygon.exterior.coords:
        general_direction = calculate_heading(plane_position, point)
        # print(general_direction, heading)
        if abs(heading - general_direction) < POINT_ACCURACY_THRESHOLD_DEG:
            verified = True

    if verified is False:
        print("not verified")
        return math.inf

    time_step = 0.1
    current_time = time_step
    while True:
        if current_time > max_time:
            return math.inf
        destination = (
            geopy.distance.geodesic(kilometers=time_step * speed / 3600)
            .destination(plane_position, heading))

        point = Point(destination.latitude, destination.longitude)
        if geofence_polygon.contains(point):
            return current_time
        current_time += time_step


planes = {}
processed_messages = 0
while True:
    check_generator_thread()
    processed_messages += process_messages(signal_generator.message_queue[:])
    calculate()
    signal_generator.message_queue = []  # Reset the messsages
    old = check_for_old_planes(time.time())
    print(planes)  # Print all generated plane data
    process_old_planes(old)
    time.sleep(0.5)
