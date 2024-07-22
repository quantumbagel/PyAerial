import json
import logging
import math
import threading

from geopy.distance import geodesic
import kafka
from kafka.errors import NoBrokersAvailable
import requests
from shapely import Polygon, Point

from constants import *

configuration = {}
zones = {}
categories = {}
backdate_packets = 0

log = logging.getLogger("Calculation")


def share_configuration_context(c, zo, cat):
    global configuration, zones, categories, backdate_packets
    configuration = c
    zones = zo
    categories = cat
    backdate_packets = configuration['general']['backdate_packets']


def get_callsign(icao):
    try:
        resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao}", timeout=1)
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        log.error(f"HEXDB API did not return 200! status:{resp.status_code}")
        return None
    json_request = resp.json()
    if "Registration" in json_request.keys():
        return json_request["Registration"]
    else:
        return None


def time_to_enter_geofence(plane_position, heading, speed, geofence_coordinates, max_time):
    # Create a shapely Polygon object from the geofence coordinates
    geofence_polygon = Polygon(geofence_coordinates)

    # Check if the current position is inside the geofence
    if geofence_polygon.contains(Point(plane_position)):
        return 0  # The plane is already inside the geofence

    verified = False
    for point in geofence_polygon.exterior.coords:
        general_direction = calculate_heading(plane_position, point)
        if abs(heading - general_direction) < configuration['general']['point_accuracy_threshold']:
            verified = True

    if verified is False:
        return math.inf

    time_step = 0.1
    current_time = time_step
    while True:
        if current_time > max_time:
            return math.inf
        destination = (
            geodesic(kilometers=time_step * speed / 3600)
            .destination(plane_position, heading))

        point = Point(destination.latitude, destination.longitude)
        if geofence_polygon.contains(point):
            return current_time
        current_time += time_step


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
    heading_degrees = ((heading_rads * 180 / math.pi) + 360) % 360
    return heading_degrees


def calculate_speed(previous, current, previous_time, current_time):
    """
    Calculate the average speed of the plane based on
     the previous position and the current position, and the time it took.
    :param previous: Previous lat/long
    :param current: Current lat/long
    :param previous_time: Previous timestamp
    :param current_time: Current timestamp
    :return: 
    """
    dist_xz = geodesic(previous, current).m
    elapsed_time = current_time - previous_time
    speed = dist_xz / elapsed_time * 3.6  # m/s to km/h
    return speed


def patch_append(plane, category, message_type, message):
    latest = get_latest(category, message_type, plane)
    if latest == message:  # duplicate
        log.debug(f"[patch_append] "
                  f"turned aside message in category {category}/{message_type}"
                  f" for plane id {plane[STORE_INFO][STORE_ICAO]}")
        return False
    log.debug(f"[patch_append] "
              f"allowed message in category {category}/{message_type}"
              f" for plane id {plane[STORE_INFO][STORE_ICAO]}")
    if message_type in plane[category].keys():
        plane[category][message_type].append(message)
        return True
    else:
        plane[category][message_type] = [message]
        return True


def get_latest(information_type, information_datum, plane_data, after_time=None):
    if information_type not in plane_data.keys():
        return None
    data = plane_data[information_type]
    if information_datum not in data.keys():
        return None
    if after_time is None:
        return data[information_datum][::-1][0]
    else:
        datum = None
        best = math.inf
        for item in data[information_datum][::-1]:
            if abs(item.time - after_time) < best:
                datum = item
            else:
                return datum
        return datum


def execute_method(method=CONFIG_CAT_ALERT_METHOD_PRINT, meta_arguments=None, method_arguments=None, payload=None):
    icao = meta_arguments[STORE_ICAO]
    tag = meta_arguments[STORE_CALLSIGN]
    message_type = meta_arguments[ALERT_CAT_TYPE]
    log.debug(f"[execute_method] "
              f"going to run method {method} with severity {message_type} on plane {icao}")
    if method == CONFIG_CAT_ALERT_METHOD_PRINT:
        print_me = {STORE_ICAO: icao, STORE_CALLSIGN: tag, ALERT_CAT_TYPE: message_type,
                    ALERT_CAT_PAYLOAD: payload, ALERT_CAT_ZONE: meta_arguments[ALERT_CAT_ZONE]}
        logger = logging.getLogger(f"{message_type}")
        logger.error(print_me)
    elif method == CONFIG_CAT_ALERT_METHOD_KAFKA:
        data = {STORE_CALLSIGN: tag, ALERT_CAT_TYPE: message_type, ALERT_CAT_PAYLOAD: payload,
                ALERT_CAT_ZONE: meta_arguments[ALERT_CAT_ZONE]}
        try:
            producer = kafka.KafkaProducer(bootstrap_servers=[method_arguments[KAFKA_METHOD_ARGUMENT_SERVER]])
            producer.send(meta_arguments[ALERT_CAT_TYPE],
                          key=bytes(icao, 'utf-8'),
                          value=bytes(json.dumps(data), 'utf-8'))
            producer.flush()
        except NoBrokersAvailable:
            log.error(f"[execute_method:kafka] Failed to send kafka message for plane {icao}! (NoBrokersAvailable)"
                      " Is the kafka server down?")


def calculate_plane(plane):
    # Check for lat/long data, which is a requirement for all advanced calculations
    if STORE_LAT not in plane[STORE_RECV_DATA].keys():  # Haven't yet received latitude/longitude packet
        return
    latitude_data = plane[STORE_RECV_DATA][STORE_LAT]
    longitude_data = plane[STORE_RECV_DATA][STORE_LONG]
    if len(latitude_data) == 1:  # Need at least two lat/long pairs to do anything
        return
    else:
        if len(latitude_data) < configuration[CONFIG_GENERAL][CONFIG_GENERAL_BACKDATE]:
            # If we don't have at least the max calc back packets,
            # set the previous to the first packet
            previous_lat = latitude_data[0]
            previous_lon = longitude_data[0]

        else:
            old_packet = len(latitude_data) - backdate_packets  # Find the indice of the old packet
            previous_lat = latitude_data[old_packet]
            previous_lon = longitude_data[old_packet]
        previous = [previous_lat.value, previous_lon.value]  # Previous lat/long
        previous_time = previous_lat.time
        current_lat = latitude_data[-1]
        current_lon = longitude_data[-1]
        current = [current_lat.value, current_lon.value]  # Current lat/long
        current_time = current_lat.time
        speed = calculate_speed(previous, current, previous_time, current_time)  # Calculate speed
        heading = calculate_heading(previous, current)  # Calculate heading
        if STORE_HORIZ_SPEED not in plane[STORE_RECV_DATA].keys():
            # If we haven't received a horiz_speed packet, don't even consider using it
            final_speed = speed
            speed_time = current_time
        else:
            horiz_plane_speed = plane[STORE_RECV_DATA][STORE_HORIZ_SPEED][-1]
            # Otherwise, get the time of the newest one
            time_ago = current_time - horiz_plane_speed.time
            if time_ago < backdate_packets:  # Is it new enough to be relevant?
                final_speed = horiz_plane_speed.value  # Use it
                speed_time = horiz_plane_speed.time
            else:
                final_speed = speed  # Not relevant, use computed speed
                speed_time = current_time

        # Add speed to data
        patch_append(plane, STORE_CALC_DATA, STORE_HORIZ_SPEED, [final_speed, speed_time])

        if STORE_HEADING not in plane[STORE_RECV_DATA].keys():  # If we don't have heading data
            final_heading = heading  # Use computed heading
        else:
            heading_data = plane[STORE_RECV_DATA][STORE_HEADING][-1]  # Get newest heading data
            time_ago = current_time - heading_data.time
            if time_ago < backdate_packets:  # Is it new enough?
                final_heading = heading_data.value  # Use it
            else:
                final_heading = heading  # Not relevant
        patch_append(plane, STORE_CALC_DATA, STORE_HEADING, [final_heading, speed_time])
        try:
            callsign = plane[STORE_INFO][STORE_CALLSIGN]
        except KeyError:
            callsign_async = threading.Thread(target=get_callsign, args=(plane,))
            callsign_async.start()
            callsign = get_callsign(plane)  # Callsign might not always exist
            if callsign is not None:  # if we got it
                plane[STORE_INFO][STORE_CALLSIGN] = callsign  # save it
            else:  # if we didn't
                # save that we failed, so we don't keep requesting data, which would slow
                # down the mainloop significantly
                plane[STORE_INFO][STORE_CALLSIGN] = ''
        geofence_etas = {}
        for geofence_name in zones:
            geofence = zones[geofence_name]
            max_time = max([geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_TIME]
                            for level in geofence[CONFIG_ZONES_LEVELS]])
            eta = time_to_enter_geofence(current, final_heading, final_speed, geofence[CONFIG_ZONES_COORDINATES],
                                         max_time)
            geofence_etas[geofence_name] = eta
            valid_levels = [level for level in geofence[CONFIG_ZONES_LEVELS]
                            if geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_TIME] <= eta]
            payload = {STORE_ALT: get_latest(STORE_RECV_DATA, STORE_ALT, plane).value,
                       STORE_LAT: get_latest(STORE_RECV_DATA, STORE_LAT, plane).value,
                       STORE_LONG: get_latest(STORE_RECV_DATA, STORE_LONG, plane).value}
            for level in valid_levels:
                reason = {CONFIG_ZONES: geofence_etas, CONFIG_ZONES_LEVELS_CATEGORY:
                          geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_CATEGORY]}
                meta_arguments = {ALERT_CAT_TYPE: level, STORE_ICAO: plane[STORE_INFO][STORE_ICAO],
                                  STORE_CALLSIGN: callsign,
                                  ALERT_CAT_REASON: reason, ALERT_CAT_ZONE: geofence_name}
                category = categories[geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_CATEGORY]]
                method_arguments = [
                    CONFIG_CAT_ALERT_ARGUMENTS] if CONFIG_CAT_ALERT_ARGUMENTS in category.keys() else None
                execute_method(method=category[CONFIG_CAT_METHOD],
                               meta_arguments=meta_arguments,
                               method_arguments=method_arguments,
                               payload=payload)
