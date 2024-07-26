
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

# Information
STORE_ICAO = "icao"
STORE_MOST_RECENT_PACKET = "last_update"
STORE_TOTAL_PACKETS = "packets"
STORE_PACKET_TYPE = "packet_type"
STORE_FIRST_PACKET = "first_packet"
STORE_CALLSIGN = "callsign"
STORE_PLANE_CATEGORY = "category"

# Configuration main categories
CONFIG_ZONES = "zones"
CONFIG_CATEGORIES = "categories"
CONFIG_GENERAL = "general"
CONFIG_HOME = "home"

# General
CONFIG_GENERAL_MONGODB = "mongodb"
CONFIG_GENERAL_BACKDATE = "backdate_packets"
CONFIG_GENERAL_REMEMBER = "remember_planes"
CONFIG_GENERAL_PAT = "point_accuracy_threshold"
CONFIG_GENERAL_PACKET_METHOD = "packet_method"
CONFIG_GENERAL_PACKET_METHOD_TRADITIONAL = "python"
CONFIG_GENERAL_PACKET_METHOD_DUMP1090 = "dump1090"
CONFIG_GENERAL_TOP_PLANES = "status_message_top_planes"
CONFIG_GENERAL_HERTZ = "hz"

INTERFACES_FOLDER = "interfaces"
CONFIG_GENERAL_PACKET_METHODS = {CONFIG_GENERAL_PACKET_METHOD_TRADITIONAL: "signal_generator",
                                 CONFIG_GENERAL_PACKET_METHOD_DUMP1090: "dump1090_tcp_interface"}

# Home
CONFIG_HOME_LATITUDE = "latitude"
CONFIG_HOME_LONGITUDE = "longitude"

# Zones
CONFIG_ZONES_COORDINATES = "coordinates"
CONFIG_ZONES_LEVELS = "levels"
CONFIG_ZONES_LEVELS_CATEGORY = "category"
CONFIG_ZONES_LEVELS_TIME = "time"
CONFIG_ZONES_LEVELS_SEND = "send"

# Categories
CONFIG_CAT_PRIORITY = "priority"
CONFIG_CAT_METHOD = "method"
CONFIG_CAT_SAVE = "save"
CONFIG_CAT_SAVE_TELEMETRY = "telemetry"
CONFIG_CAT_SAVE_TELEMETRY_METHOD = "telemetry_method"
CONFIG_CAT_SAVE_CALCULATED = "calculated"
CONFIG_CAT_SAVE_CALCULATED_METHOD = "calculated_method"
CONFIG_CAT_SAVE_PACKET = "packet"

# Alert methods
CONFIG_CAT_ALERT_METHOD_PRINT = "print"
CONFIG_CAT_ALERT_METHOD_KAFKA = "kafka"

CONFIG_CAT_ALERT_ARGUMENTS = "arguments"

# Save methods
CONFIG_CAT_SAVE_METHOD_DECIMATE = "decimate"
CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE = "sdecimate"
CONFIG_CAT_SAVE_METHOD_ALL = "all"
CONFIG_CAT_SAVE_METHOD_NONE = "none"


# Alert messaging

ALERT_CAT_TYPE = "type"
ALERT_CAT_REASON = "reason"
ALERT_CAT_ZONE = "zone"
ALERT_CAT_PAYLOAD = "payload"


# Database

STORAGE_CATEGORY = "category"
STORAGE_DATA_TYPE = "type"
STORAGE_DATA = "data"
STORAGE_LEVEL = "level"
STORAGE_ZONE = "zone"

KAFKA_METHOD_ARGUMENT_SERVER = "server"

CONFIGURATION = {}  # This configuration will be loaded by pyaerial.py on startup.
