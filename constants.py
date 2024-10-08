"""
Constants used by all PyAerial modules
"""
import operator

CONFIG_FILE = "config.yaml"

# Data types
STORE_INFO = "info"
STORE_RECV_DATA = "received_data"
STORE_CALC_DATA = "calculated_data"
STORE_INTERNAL = "internal"

# Received data
STORE_LAT = "latitude"
STORE_LONG = "longitude"
STORE_ALT = "altitude"
STORE_VERT_SPEED = "vertical_speed"

# Received / calculated data
STORE_HORIZ_SPEED = "horizontal_speed"
STORE_HEADING = "direction"
STORE_DISTANCE = "distance"
# Information
STORE_ICAO = "icao"
STORE_MOST_RECENT_PACKET = "last_update"
STORE_TOTAL_PACKETS = "packets"
STORE_PACKET_TYPE = "packet_type"
STORE_FIRST_PACKET = "first_packet"
STORE_CALLSIGN = "callsign"
STORE_PLANE_CATEGORY = "plane_category"

STORE_DATA_TYPES = {STORE_RECV_DATA: [STORE_LAT, STORE_ALT, STORE_LONG, STORE_VERT_SPEED, STORE_HORIZ_SPEED, STORE_HEADING],
                    STORE_CALC_DATA: [STORE_HORIZ_SPEED, STORE_HEADING]}

STORE_DATA_CONFIG_NAMING = {STORE_RECV_DATA: "telemetry", STORE_CALC_DATA: "calculated"}

STORE_PLANE_CATEGORY_CONVERSION = {2: {1: "Surface Emergency Vehicle", 3: "Surface Service Vehicle",
                                       4: "Ground Obstruction (4)", 5: "Ground Obstruction (5)",
                                       6: "Ground Obstruction (6)", 7: "Ground Obstruction (7)"},
                                   3: {1: "Glider/Sailplane", 2: "Lighter-than-air", 3: "Parachutist/Skydiver",
                                       4: "Ultralight/Hang-glider/paraglider", 6: "UAV (unmanned aerial vehicle)",
                                       7: "Space/transatmospheric vehicle"},
                                   4: {1: "Light (<7000kg)", 2: "Medium 1 (7000 to 34000kg)",
                                       3: "Medium 2 (34000 to 136000kg)",
                                       4: "High vortex aircraft", 5: "Heavy (>13600kg)",
                                       6: "High performance (>5g) and high speed (>740km/h)",
                                       7: "Rotorcraft (helicopter)"}}

STORE_OPENSKY_HEADER = ["icao", "timestamp", "acars", "adsb", "built", "description", "country", "engines",
                      "first_flight_date", "first_seen", "icao_aircraft_class", "line_number", "manufacturer_icao",
                      "manufacturer_name", "model", "modes", "next_registration", "operator", "operator_callsign",
                      "operator_data", "operator_iata", "owner", "previous_registration", "registered_until",
                      "registered", "callsign", "selective_calling_number", "serial_number", "status", "typecode",
                      "vhf"]

STORE_PIPELINE_LAST_RETURN = "last_return"
STORE_PIPELINE_MESSAGES = "messages"
# Configuration main categories
CONFIG_ZONES = "zones"
CONFIG_CATEGORIES = "categories"
CONFIG_GENERAL = "general"
CONFIG_HOME = "home"
CONFIG_COMPONENTS = "components"

# General
CONFIG_GENERAL_MONGODB = "mongodb"
CONFIG_GENERAL_MERGE_PACKETS = "duplicate_packet_merging"
CONFIG_GENERAL_BACKDATE = "backdate_packets"
CONFIG_GENERAL_REMEMBER = "remember_planes"
CONFIG_GENERAL_TOP_PLANES = "status_message_top_planes"
CONFIG_GENERAL_ADVANCED_STATUS = "advanced_status"
CONFIG_GENERAL_HERTZ = "hz"
CONFIG_GENERAL_LOGGING_LEVEL = "logs"

LOGGING_LEVELS = {"debug": 10,
                  "info": 20,
                  "warning": 30,
                  "error": 40}

INTERFACES_FOLDER = "interfaces"


# Home
CONFIG_HOME_LATITUDE = "latitude"
CONFIG_HOME_LONGITUDE = "longitude"

# Zones
CONFIG_ZONES_COORDINATES = "coordinates"
CONFIG_ZONES_LEVELS = "levels"
CONFIG_ZONES_LEVELS_CATEGORY = "category"
CONFIG_ZONES_LEVELS_REQUIREMENTS = "requirements"
CONFIG_ZONES_LEVELS_SECONDS = "seconds"

# Categories
CONFIG_CAT_METHOD = "alert_method"
CONFIG_CAT_SAVE = "save"
CONFIG_CAT_SAVE_TELEMETRY_METHOD = "telemetry_method"
CONFIG_CAT_SAVE_CALCULATED_METHOD = "calculated_method"

CONFIG_CAT_SAVE_METHOD_TYPES = [CONFIG_CAT_SAVE_TELEMETRY_METHOD, CONFIG_CAT_SAVE_CALCULATED_METHOD]

# Alert messaging

ALERT_CAT_TYPE = "type"
ALERT_CAT_REASON = "reason"
ALERT_CAT_ZONE = "zone"
ALERT_CAT_PAYLOAD = "payload"
ALERT_CAT_ETA = "eta"

# Components

CONFIG_COMP_TYPES = [STORE_LAT, STORE_LONG, STORE_ALT, STORE_VERT_SPEED, STORE_HORIZ_SPEED, STORE_HEADING, STORE_DISTANCE,
                     ALERT_CAT_ETA]  # TODO: Add "seen" as a data type

# CTYPE = comparison type
CONFIG_COMP_CTYPE_MINIMUM = "minimum"
CONFIG_COMP_CTYPE_MAXIMUM = "maximum"
CONFIG_COMP_CTYPES = {CONFIG_COMP_CTYPE_MAXIMUM: [STORE_LAT, STORE_LONG, STORE_VERT_SPEED, STORE_HORIZ_SPEED,
                                                  STORE_HEADING, STORE_DISTANCE, ALERT_CAT_ETA],
                      CONFIG_COMP_CTYPE_MINIMUM: [STORE_LAT, STORE_LONG, STORE_VERT_SPEED, STORE_HORIZ_SPEED,
                                                  STORE_HEADING, STORE_DISTANCE, ALERT_CAT_ETA]}
CONFIG_COMP_FUNCTIONS = {CONFIG_COMP_CTYPE_MAXIMUM: operator.le, CONFIG_COMP_CTYPE_MINIMUM: operator.ge}


# Alert methods
CONFIG_CAT_ALERT_METHOD_PRINT = "print"
CONFIG_CAT_ALERT_METHOD_KAFKA = "kafka"

CONFIG_CAT_ALERT_METHODS = {CONFIG_CAT_ALERT_METHOD_KAFKA: {"server": True},
                            CONFIG_CAT_ALERT_METHOD_PRINT: {}}
CONFIG_CAT_ALERT_ARGUMENTS = "arguments"

# Save methods
CONFIG_CAT_SAVE_METHOD_DECIMATE = "decimate"
CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE = "sdecimate"
CONFIG_CAT_SAVE_METHOD_ALL = "all"
CONFIG_CAT_SAVE_METHOD_NONE = "none"
CONFIG_CAT_DEFAULT_SAVE_METHOD = "default"

CONFIG_CAT_SAVE_METHODS = {CONFIG_CAT_SAVE_METHOD_DECIMATE: 1,
                           CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE: 2,
                           CONFIG_CAT_SAVE_METHOD_ALL: 0,
                           CONFIG_CAT_SAVE_METHOD_NONE: 0}

# Receivers

CONFIG_RECEIVERS = "receivers"
CONFIG_RECV_METHOD = "method"
CONFIG_RECV_METHOD_ARGUMENT_RTL_INDEX = "rtl_index"
CONFIG_RECV_METHOD_ARGUMENT_TCP_CONNECTION_IP = "tcp_connection_ip"
CONFIG_RECV_METHOD_ARGUMENT_TCP_CONNECTION_PORT = "tcp_connection_port"

CONFIG_RECV_ARGUMENTS = "arguments"
CONFIG_RECV_METHODS = {"py1090": {CONFIG_RECV_METHOD_ARGUMENT_RTL_INDEX: str},
                       "dump1090": {CONFIG_RECV_METHOD_ARGUMENT_TCP_CONNECTION_IP: str,
                                    CONFIG_RECV_METHOD_ARGUMENT_TCP_CONNECTION_PORT: str}}

# Database

STORAGE_CATEGORY = "category"
STORAGE_DATA_TYPE = "type"
STORAGE_DATA = "data"
STORAGE_LEVEL = "level"
STORAGE_ZONE = "zone"

KAFKA_METHOD_ARGUMENT_SERVER = "server"

CONFIGURATION = {}  # This configuration will be loaded by the main thread on startup
