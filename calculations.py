"""
Performs the data analysis / aggregation for PyAerial.
"""
import ast
import io
import json
import logging
import math
import threading
import csv
from geopy.distance import geodesic
import kafka
from kafka.errors import NoBrokersAvailable
import requests
from shapely import Polygon, Point, LineString
from shapely.ops import nearest_points

import constants
import helpers
from constants import *

configuration = constants.CONFIGURATION
zones = configuration[CONFIG_ZONES]
categories = configuration[CONFIG_CATEGORIES]
backdate_packets = configuration[CONFIG_GENERAL][CONFIG_GENERAL_BACKDATE]

main_logger = logging.getLogger("Calculation")


def get_callsign(icao: str) -> str | None:
    """
    Attempts to use the HEXDB API to convert between ICAO hex and flight tracker ID.
    It seems like HEXDB is currently broken however, this function may be removed in future versions.
    :param icao: ICAO hex string
    :return: flight tracker ID
    """
    log = main_logger.getChild("get_callsign")
    try:
        resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao}", timeout=1)  # Make the request
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        log.debug(f"HEXDB API did not return 200! status:{resp.status_code}")  # API failed (happening a lot lol)
        return None
    json_request = resp.json()
    # Return response - if it's valid
    return json_request["Registration"] if "Registration" in json_request.keys() else None


def get_airplane_info(icao: str) -> dict | None:
    """
    Attempts to use the OpenSky Network to get more information about the ICAO
    :param icao: ICAO hex string
    :return: OpenSky Network airplane information
    """

    for line in open("database.csv"):
        if line.startswith(icao):
            plane = list(csv.reader(io.StringIO(line), quotechar="'"))[0]
            data = {}
            for ind, thing in enumerate(plane):
                data[STORE_OPENSKY_HEADER[ind]] = thing
            return data
    return None


def time_to_enter_geofence(plane_position: list[float],
                           heading: float, speed: float,
                           geofence_coordinates: list[list[float]], max_time: int) -> float:
    """
    Calculate the time a plane, with heading and speed, will take to enter a geofence, if at all.

    :param plane_position: Position of the plane (latitude, longitude)
    :param heading: Heading of the plane, in degrees from true north
    :param speed: Speed of the plane, in km/h
    :param geofence_coordinates: List of coordinates of the geofence (latitude, longitude)
    :param max_time: Maximum time the plane can take to enter the geofence, in seconds

    :return: the time in seconds until the plane arrives, or math.inf if it doesn't
    """
    # Create a shapely Polygon object from the geofence coordinates

    geofence_polygon = Polygon(geofence_coordinates)

    # Check if the current position is inside the geofence
    if geofence_polygon.contains(Point(plane_position)):
        return 0  # The plane is already inside the geofence

    distance_approx = max_time * speed / 3600
    destination = (
        geodesic(kilometers=distance_approx)
        .destination(plane_position, heading))  # Where will the plane be in max_time
    # The line through the maximum possible distance
    line = LineString([Point(plane_position), Point(destination.latitude, destination.longitude)])
    # Where are the intersection points?
    intersect: LineString = line.intersection(geofence_polygon)
    if not len(intersect.coords):  # No intersection
        return math.inf
    close = list(intersect.coords)[0]  # Get the close one
    distance = geodesic(plane_position, close)  # Calculate the net distance
    return distance.km / speed * 3600  # How long until we get there?


def calculate_heading(previous, current):
    """
    Calculate the direction of the heading from the lat long pair previous to the lat long pair current.
    :param previous: The previous position (lat/long)
    :param current: The current position (lat/long)
    :return: The heading (degrees)
    """

    pi_c = math.pi / 180

    # Get latitude and longitude, and convert to radians
    first_lat = previous[0] * pi_c
    first_lon = previous[1] * pi_c
    second_lat = current[0] * pi_c
    second_lon = current[1] * pi_c

    # Cursed math
    y = math.sin(second_lon - first_lon) * math.cos(second_lat)
    x = (math.cos(first_lat) * math.sin(second_lat)) - (
            math.sin(first_lat) * math.cos(second_lat) * math.cos(second_lon - first_lon))
    heading_rads = math.atan2(y, x)

    heading_degrees = ((heading_rads * 180 / math.pi) + 360) % 360  # Convert back to degrees
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


def patch_append(plane: dict, category: str, message_type: str, message: helpers.Datum):
    """
    Adds a new message to the given plane with the given category and message type iff it doesn't already exist.

    :param plane: Plane to add data to
    :param category: Category to add data to
    :param message_type: Message type to add to (within category)
    :param message: Message to add within the message type and category
    :return: if the message was added
    """
    latest = get_latest(category, message_type, plane)
    if latest == message:  # duplicate
        return False

    if message_type in plane[category].keys():  # Ensure the location exists
        plane[category][message_type].append(message)
        return True
    else:
        plane[category][message_type] = [message]
        return True


def get_latest(information_type: str, information_datum: str, plane_data: dict, after_time: float = None) \
        -> helpers.Datum | None:
    """
    Get the latest packet in a certain type/datum combination

    :param information_type: Type of information
    :param information_datum: Datum of information (subcategory)
    :param plane_data: Plane data where the information is
    :param after_time: If set, the function will find the most recent packet before this timestamp.
    """
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
                best = abs(item.time - after_time)
            else:
                return datum
        return datum


def execute_method(method: str = CONFIG_CAT_ALERT_METHOD_PRINT,
                   meta_arguments: dict = None,
                   method_arguments: dict = None,
                   payload: dict = None) -> None:
    """
    Execute the given alert method with the given arguments.

    :param method: Method to execute (print, kafka)
    :param meta_arguments: Meta arguments to pass to the method (stuff like zone, plane id).
     Basically stuff that is shared between different types of alerts.
    :param method_arguments: Specific arguments for the type of method.
    :param payload: Payload to pass to the method. This contains the current position and altitude of the plane.

    :return: None
    """
    log = main_logger.getChild("execute_method")
    icao = meta_arguments[STORE_ICAO]
    tag = meta_arguments[STORE_CALLSIGN]
    message_type = meta_arguments[ALERT_CAT_TYPE]
    log.debug(f"going to run method {method} with severity {message_type} on plane {icao}")
    if method == CONFIG_CAT_ALERT_METHOD_PRINT:
        print_me = {STORE_ICAO: icao, STORE_CALLSIGN: tag, STORE_OPENSKY: meta_arguments[STORE_OPENSKY],
                    ALERT_CAT_TYPE: message_type,
                    ALERT_CAT_PAYLOAD: payload, ALERT_CAT_ZONE: meta_arguments[ALERT_CAT_ZONE],
                    ALERT_CAT_ETA: meta_arguments[ALERT_CAT_ETA]}
        logger = logging.getLogger(f"{message_type}")
        logger.debug(print_me)
    elif method == CONFIG_CAT_ALERT_METHOD_KAFKA:
        data = {STORE_CALLSIGN: tag, STORE_OPENSKY: meta_arguments[STORE_OPENSKY], ALERT_CAT_TYPE: message_type,
                ALERT_CAT_PAYLOAD: payload,
                ALERT_CAT_ZONE: meta_arguments[ALERT_CAT_ZONE], ALERT_CAT_ETA: meta_arguments[ALERT_CAT_ETA]}
        try:
            producer = kafka.KafkaProducer(bootstrap_servers=[method_arguments[KAFKA_METHOD_ARGUMENT_SERVER]])
            producer.send(meta_arguments[ALERT_CAT_TYPE],
                          key=bytes(icao, 'utf-8'),
                          value=bytes(json.dumps(data), 'utf-8'))
            producer.flush()
        except NoBrokersAvailable:
            log.error(f"Failed to send kafka message for plane {icao}! (NoBrokersAvailable)"
                      " Is the kafka server down?")


def calculate_plane(plane: dict) -> None:
    """
    Run calculations on the given plane's data and add the calculated data to the plane's data.
    :param plane: Plane data
    :return: None
    """
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
            # Get the most accurate data/time pairing
            previous_lon = get_latest(STORE_RECV_DATA, STORE_LAT, plane, previous_lat.time)
        previous = [previous_lat.value, previous_lon.value]  # Previous lat/long
        previous_time = previous_lat.time
        current_lat: helpers.Datum = latitude_data[-1]
        current_lon: helpers.Datum = longitude_data[-1]
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
        patch_append(plane, STORE_CALC_DATA, STORE_HORIZ_SPEED, helpers.Datum(final_speed, speed_time))

        if STORE_HEADING not in plane[STORE_RECV_DATA].keys():  # If we don't have heading data
            final_heading = heading  # Use computed heading
        else:
            heading_data = plane[STORE_RECV_DATA][STORE_HEADING][-1]  # Get newest heading data
            time_ago = current_time - heading_data.time
            if time_ago < backdate_packets:  # Is it new enough?
                final_heading = heading_data.value  # Use it
            else:
                final_heading = heading  # Not relevant
        patch_append(plane, STORE_CALC_DATA, STORE_HEADING, helpers.Datum(final_heading, speed_time))

        # Callsign logic
        try:
            callsign = plane[STORE_INFO][STORE_CALLSIGN]
        except KeyError:
            callsign = get_callsign(plane[STORE_INFO][STORE_ICAO])  # Callsign might not always exist
            if callsign is not None:  # if we got it
                plane[STORE_INFO][STORE_CALLSIGN] = callsign  # save it
            else:  # if we didn't
                # save that we failed, so we don't keep requesting data, which would slow
                # down the mainloop significantly
                plane[STORE_INFO][STORE_CALLSIGN] = ''

        # OpenSky logic
        try:
            opensky_information = plane[STORE_INFO][STORE_OPENSKY]
        except KeyError:
            opensky_information = get_airplane_info(plane[STORE_INFO][STORE_ICAO].lower())
            if opensky_information is not None:
                if opensky_information is not None:  # if we got it
                    plane[STORE_INFO][STORE_OPENSKY] = opensky_information  # save it
                else:  # if we didn't
                    # save that we failed, so we don't keep requesting data, which would slow
                    # down the mainloop less significantly
                    plane[STORE_INFO][STORE_OPENSKY] = None

        # This section of the code will take the information we've gathered and determine what alerts should be sent.
        geofence_etas = {}
        for geofence_name in zones:
            geofence = zones[geofence_name]

            # Figure out the category that requires the most scanning within the zone.
            eta = time_to_enter_geofence(current, final_heading, final_speed, geofence[CONFIG_ZONES_COORDINATES],
                                         10000)  # How long will it take?
            # max_time's number doesn't technically matter, as long as it's large. This is because of the Shapely
            # cartesian algorithm
            geofence_etas[geofence_name] = eta

            # Determine the levels within the zone that qualify
            valid_levels = []
            for level in geofence[CONFIG_ZONES_LEVELS]:
                requirements = geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_REQUIREMENTS]
                component_names = [node.id for node in ast.walk(ast.parse(requirements))
                                   if type(node) is ast.Name]
                components = {}
                # Scan through and evaluate each component within the requirement
                for component_name in component_names:
                    component_failed = False
                    component = configuration[CONFIG_COMPONENTS][component_name]  # The data within the component
                    for data_type in component.keys():  # For each data type, find the piece of data that's relevant
                        relevant_data = None

                        if data_type in [STORE_LAT, STORE_LONG, STORE_ALT, STORE_VERT_SPEED]:  # Received data
                            relevant_data = get_latest(STORE_RECV_DATA, data_type, plane).value
                        elif data_type in [STORE_HORIZ_SPEED, STORE_HEADING]:  # Calculated data
                            relevant_data = get_latest(STORE_CALC_DATA, data_type, plane).value
                        elif data_type == ALERT_CAT_ETA:  # ETA
                            relevant_data = eta
                        elif data_type == STORE_DISTANCE:  # Distance
                            points = nearest_points(Polygon(geofence[CONFIG_ZONES_COORDINATES]), Point(current))
                            relevant_data = geodesic((points[0].latitude, points[0].longitude), current)

                        if relevant_data is None:  # If there isn't valid data, fail the component
                            component_failed = True
                            break

                        for comparison in component[data_type].keys():
                            # Has our component failed?
                            if not CONFIG_COMP_FUNCTIONS[comparison](relevant_data, component[data_type][comparison]):
                                component_failed = True
                                break

                        if component_failed:
                            break

                    components[component_name] = not component_failed  # Did the component succeed?

                if eval(requirements, components):  # Evaluate
                    valid_levels.append(level)

            payload = {STORE_ALT: get_latest(STORE_RECV_DATA, STORE_ALT, plane).value,
                       STORE_LAT: get_latest(STORE_RECV_DATA, STORE_LAT, plane).value,
                       STORE_LONG: get_latest(STORE_RECV_DATA, STORE_LONG, plane).value}

            for level in valid_levels:  # Send alert messages to each of the categories that qualify
                reason = {CONFIG_ZONES: geofence_etas, CONFIG_ZONES_LEVELS_CATEGORY:
                          geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_CATEGORY]}

                meta_arguments = {ALERT_CAT_TYPE: level, STORE_ICAO: plane[STORE_INFO][STORE_ICAO],
                                  STORE_CALLSIGN: callsign,
                                  ALERT_CAT_REASON: reason, ALERT_CAT_ZONE: geofence_name,
                                  ALERT_CAT_ETA: eta,
                                  STORE_OPENSKY: opensky_information}

                category = geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_CATEGORY]
                if type(category) is str:  # Contain reference or actual category data?
                    category = categories[geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_CATEGORY]]

                # Compile method_arguments if we need them
                method_arguments = category[CONFIG_CAT_ALERT_ARGUMENTS] \
                    if CONFIG_CAT_ALERT_ARGUMENTS in category.keys() else None

                # Actually execute the method
                execute_method(method=category[CONFIG_CAT_METHOD],
                               meta_arguments=meta_arguments,
                               method_arguments=method_arguments,
                               payload=payload)
