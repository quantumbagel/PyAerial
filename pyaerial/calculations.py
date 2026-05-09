"""
Data analysis / aggregation for PyAerial.
"""
from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor

import requests
from geopy.distance import geodesic
from shapely import LineString, Point, Polygon
from shapely.ops import nearest_points

from pyaerial import helpers
from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_PAYLOAD,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    CONFIGURATION,
    CONFIG_CAT_ALERT_ARGUMENTS,
    CONFIG_CAT_ALERT_METHOD_KAFKA,
    CONFIG_CAT_ALERT_METHOD_PRINT,
    CONFIG_CAT_METHOD,
    CONFIG_COMPONENTS,
    CONFIG_COMP_FUNCTIONS,
    CONFIG_GENERAL,
    CONFIG_GENERAL_BACKDATE,
    CONFIG_ZONES,
    CONFIG_ZONES_COORDINATES,
    CONFIG_ZONES_LEVELS,
    CONFIG_ZONES_LEVELS_CATEGORY,
    CONFIG_ZONES_LEVELS_REQUIREMENTS,
    KAFKA_METHOD_ARGUMENT_SERVER,
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
    STORE_VERT_SPEED,
    STORE_DISTANCE,
)
from pyaerial.requirements_eval import collect_component_names, eval_requirement

main_logger = logging.getLogger("calculation")

_callsign_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hexdb")
_callsign_futures: dict[str, object] = {}


def _get_callsign_sync(icao: str) -> str | None:
    try:
        resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao}", timeout=1)
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        main_logger.getChild("get_callsign").debug("HEXDB status %s", resp.status_code)
        return None
    json_request = resp.json()
    return json_request["Registration"] if "Registration" in json_request else None


def get_callsign(icao: str) -> str | None:
    """Resolve registration/callsign via HEXDB (blocking). Prefer ensure_callsign for the main loop."""
    return _get_callsign_sync(icao)


def ensure_callsign_async(plane: dict) -> None:
    """
    Non-blocking HEXDB lookup: submit once per ICAO, apply result when the future completes.
    """
    icao = plane[STORE_INFO][STORE_ICAO]
    if STORE_CALLSIGN in plane[STORE_INFO]:
        return
    if icao not in _callsign_futures:
        _callsign_futures[icao] = _callsign_executor.submit(_get_callsign_sync, icao)
    fut = _callsign_futures[icao]
    if not fut.done():
        return
    try:
        result = fut.result()
        plane[STORE_INFO][STORE_CALLSIGN] = result if result is not None else ""
    except Exception:
        plane[STORE_INFO][STORE_CALLSIGN] = ""
    finally:
        _callsign_futures.pop(icao, None)


def time_to_enter_geofence(
    plane_position: list[float],
    heading: float,
    speed: float,
    geofence_coordinates: list[list[float]],
    max_time: int,
) -> float:
    geofence_polygon = Polygon(geofence_coordinates)
    if geofence_polygon.contains(Point(plane_position)):
        return 0
    if speed <= 0:
        return math.inf

    distance_approx = max_time * speed / 3600
    destination = geodesic(kilometers=distance_approx).destination(plane_position, heading)
    line = LineString([Point(plane_position), Point(destination.latitude, destination.longitude)])
    intersect = line.intersection(geofence_polygon)
    if not len(intersect.coords):
        return math.inf
    close = list(intersect.coords)[0]
    distance = geodesic(plane_position, close)
    return distance.km / speed * 3600


def calculate_heading(previous, current):
    pi_c = math.pi / 180
    first_lat = previous[0] * pi_c
    first_lon = previous[1] * pi_c
    second_lat = current[0] * pi_c
    second_lon = current[1] * pi_c

    y = math.sin(second_lon - first_lon) * math.cos(second_lat)
    x = (math.cos(first_lat) * math.sin(second_lat)) - (
        math.sin(first_lat) * math.cos(second_lat) * math.cos(second_lon - first_lon)
    )
    heading_rads = math.atan2(y, x)
    return ((heading_rads * 180 / math.pi) + 360) % 360


def calculate_speed(previous, current, previous_time, current_time):
    dist_xz = geodesic(previous, current).m
    elapsed_time = current_time - previous_time
    if elapsed_time <= 0:
        return 0.0
    return dist_xz / elapsed_time * 3.6


def patch_append(plane: dict, category: str, message_type: str, message: helpers.Datum):
    latest = get_latest(category, message_type, plane)
    if latest == message:
        return False
    if message_type in plane[category]:
        plane[category][message_type].append(message)
        return True
    plane[category][message_type] = [message]
    return True


def get_latest(
    information_type: str, information_datum: str, plane_data: dict, after_time: float | None = None
) -> helpers.Datum | None:
    if information_type not in plane_data:
        return None
    data = plane_data[information_type]
    if information_datum not in data:
        return None
    if after_time is None:
        return data[information_datum][::-1][0]
    datum = None
    best = math.inf
    for item in data[information_datum][::-1]:
        if abs(item.time - after_time) < best:
            datum = item
            best = abs(item.time - after_time)
        else:
            return datum
    return datum


def execute_method(
    method: str = CONFIG_CAT_ALERT_METHOD_PRINT,
    meta_arguments: dict | None = None,
    method_arguments: dict | None = None,
    payload: dict | None = None,
) -> None:
    log = main_logger.getChild("execute_method")
    meta_arguments = meta_arguments or {}
    icao = meta_arguments[STORE_ICAO]
    tag = meta_arguments[STORE_CALLSIGN]
    message_type = meta_arguments[ALERT_CAT_TYPE]
    log.debug("going to run method %s with severity %s on plane %s", method, message_type, icao)
    if method == CONFIG_CAT_ALERT_METHOD_PRINT:
        print_me = {
            STORE_ICAO: icao,
            STORE_CALLSIGN: tag,
            ALERT_CAT_TYPE: message_type,
            ALERT_CAT_PAYLOAD: payload,
            ALERT_CAT_ZONE: meta_arguments[ALERT_CAT_ZONE],
            ALERT_CAT_ETA: meta_arguments[ALERT_CAT_ETA],
        }
        logging.getLogger(str(message_type)).debug("%s", print_me)
    elif method == CONFIG_CAT_ALERT_METHOD_KAFKA:
        import kafka
        from kafka.errors import NoBrokersAvailable

        data = {
            STORE_CALLSIGN: tag,
            ALERT_CAT_TYPE: message_type,
            ALERT_CAT_PAYLOAD: payload,
            ALERT_CAT_ZONE: meta_arguments[ALERT_CAT_ZONE],
            ALERT_CAT_ETA: meta_arguments[ALERT_CAT_ETA],
        }
        try:
            producer = kafka.KafkaProducer(
                bootstrap_servers=[method_arguments[KAFKA_METHOD_ARGUMENT_SERVER]]
            )
            producer.send(
                meta_arguments[ALERT_CAT_TYPE],
                key=bytes(icao, "utf-8"),
                value=bytes(json.dumps(data), "utf-8"),
            )
            producer.flush()
        except NoBrokersAvailable:
            log.error("Kafka NoBrokersAvailable for plane %s", icao)


def _refresh_config_views():
    global configuration, zones, categories, backdate_packets
    configuration = CONFIGURATION
    zones = configuration[CONFIG_ZONES]
    categories = configuration[CONFIG_CATEGORIES]
    backdate_packets = configuration[CONFIG_GENERAL][CONFIG_GENERAL_BACKDATE]


configuration = CONFIGURATION
zones = {}
categories = {}
backdate_packets = 10


def calculate_plane(plane: dict) -> None:
    _refresh_config_views()
    if STORE_LAT not in plane[STORE_RECV_DATA]:
        return
    latitude_data = plane[STORE_RECV_DATA][STORE_LAT]
    longitude_data = plane[STORE_RECV_DATA][STORE_LONG]
    if len(latitude_data) == 1:
        return

    if len(latitude_data) < configuration[CONFIG_GENERAL][CONFIG_GENERAL_BACKDATE]:
        previous_lat = latitude_data[0]
        previous_lon = longitude_data[0]
    else:
        old_packet = len(latitude_data) - backdate_packets
        previous_lat = latitude_data[old_packet]
        previous_lon = get_latest(STORE_RECV_DATA, STORE_LONG, plane, previous_lat.time)

    previous = [previous_lat.value, previous_lon.value]
    previous_time = previous_lat.time
    current_lat: helpers.Datum = latitude_data[-1]
    current_lon: helpers.Datum = longitude_data[-1]
    current = [current_lat.value, current_lon.value]
    current_time = current_lat.time
    speed = calculate_speed(previous, current, previous_time, current_time)
    heading = calculate_heading(previous, current)

    if STORE_HORIZ_SPEED not in plane[STORE_RECV_DATA]:
        final_speed = speed
        speed_time = current_time
    else:
        horiz_plane_speed = plane[STORE_RECV_DATA][STORE_HORIZ_SPEED][-1]
        time_ago = current_time - horiz_plane_speed.time
        if time_ago < backdate_packets:
            final_speed = horiz_plane_speed.value
            speed_time = horiz_plane_speed.time
        else:
            final_speed = speed
            speed_time = current_time

    patch_append(plane, STORE_CALC_DATA, STORE_HORIZ_SPEED, helpers.Datum(final_speed, speed_time))

    if STORE_HEADING not in plane[STORE_RECV_DATA]:
        final_heading = heading
    else:
        heading_data = plane[STORE_RECV_DATA][STORE_HEADING][-1]
        time_ago = current_time - heading_data.time
        final_heading = heading_data.value if time_ago < backdate_packets else heading
    patch_append(plane, STORE_CALC_DATA, STORE_HEADING, helpers.Datum(final_heading, speed_time))

    if STORE_CALLSIGN not in plane[STORE_INFO]:
        ensure_callsign_async(plane)

    try:
        callsign = plane[STORE_INFO][STORE_CALLSIGN]
    except KeyError:
        callsign = ""

    geofence_etas = {}
    for geofence_name in zones:
        geofence = zones[geofence_name]
        eta = time_to_enter_geofence(
            current, final_heading, final_speed, geofence[CONFIG_ZONES_COORDINATES], 10000
        )
        geofence_etas[geofence_name] = eta

        valid_levels = []
        for level in geofence[CONFIG_ZONES_LEVELS]:
            requirements = geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_REQUIREMENTS]
            component_names = collect_component_names(requirements)
            components = {}
            for component_name in component_names:
                component_failed = False
                component = configuration[CONFIG_COMPONENTS][component_name]
                for data_type in component:
                    relevant_data = None

                    if data_type in (STORE_LAT, STORE_LONG, STORE_ALT, STORE_VERT_SPEED):
                        latest = get_latest(STORE_RECV_DATA, data_type, plane)
                        relevant_data = latest.value if latest else None
                    elif data_type in (STORE_HORIZ_SPEED, STORE_HEADING):
                        latest = get_latest(STORE_CALC_DATA, data_type, plane)
                        relevant_data = latest.value if latest else None
                    elif data_type == ALERT_CAT_ETA:
                        relevant_data = eta
                    elif data_type == STORE_DISTANCE:
                        points = nearest_points(
                            Polygon(geofence[CONFIG_ZONES_COORDINATES]), Point(current)
                        )
                        pt = points[0]
                        relevant_data = geodesic((pt.x, pt.y), tuple(current)).km

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
                valid_levels.append(level)

        lat_d = get_latest(STORE_RECV_DATA, STORE_LAT, plane)
        lon_d = get_latest(STORE_RECV_DATA, STORE_LONG, plane)
        alt_d = get_latest(STORE_RECV_DATA, STORE_ALT, plane)
        payload = {
            STORE_ALT: alt_d.value if alt_d else None,
            STORE_LAT: lat_d.value if lat_d else None,
            STORE_LONG: lon_d.value if lon_d else None,
        }

        for level in valid_levels:
            reason = {
                CONFIG_ZONES: geofence_etas,
                CONFIG_ZONES_LEVELS_CATEGORY: geofence[CONFIG_ZONES_LEVELS][level][
                    CONFIG_ZONES_LEVELS_CATEGORY
                ],
            }
            meta_arguments = {
                ALERT_CAT_TYPE: level,
                STORE_ICAO: plane[STORE_INFO][STORE_ICAO],
                STORE_CALLSIGN: callsign,
                ALERT_CAT_REASON: reason,
                ALERT_CAT_ZONE: geofence_name,
                ALERT_CAT_ETA: eta,
            }

            category = geofence[CONFIG_ZONES_LEVELS][level][CONFIG_ZONES_LEVELS_CATEGORY]
            if isinstance(category, str):
                category = categories[category]

            method_arguments = (
                category[CONFIG_CAT_ALERT_ARGUMENTS]
                if CONFIG_CAT_ALERT_ARGUMENTS in category
                else None
            )

            execute_method(
                method=category[CONFIG_CAT_METHOD],
                meta_arguments=meta_arguments,
                method_arguments=method_arguments,
                payload=payload,
            )
