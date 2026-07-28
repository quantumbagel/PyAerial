"""
Domain constants shared across PyAerial modules.

These are stable identifiers for data fields, message categories, and database
keys. Anything that is user-configurable now lives in the typed configuration
schema (:mod:`pyaerial.config`) rather than here.
"""
import operator

# --- Top-level buckets stored per plane ---------------------------------------
STORE_INFO = "info"
STORE_RECV_DATA = "received_data"
STORE_CALC_DATA = "calculated_data"
STORE_INTERNAL = "internal"

# --- Telemetry / calculated data fields ---------------------------------------
STORE_LAT = "latitude"
STORE_LONG = "longitude"
STORE_ALT = "altitude"
STORE_VERT_SPEED = "vertical_speed"
STORE_HORIZ_SPEED = "speed"
STORE_HEADING = "heading"
STORE_SELECTED_HEADING = "selected_heading"
STORE_SELECTED_ALTITUDE = "selected_altitude"
STORE_DISTANCE = "distance"

# --- Plane information fields --------------------------------------------------
STORE_ICAO = "icao"
STORE_CALLSIGN = "callsign"
STORE_PLANE_CATEGORY = "plane_category"

# --- Internal bookkeeping fields ----------------------------------------------
STORE_MOST_RECENT_PACKET = "last_update"
STORE_TOTAL_PACKETS = "packets"
STORE_PACKET_TYPE = "packet_type"
STORE_FIRST_PACKET = "first_packet"
STORE_PORTAL_PROJECTION = "portal_projection"

# --- Alert payload fields -----------------------------------------------------
ALERT_CAT_TYPE = "type"
ALERT_CAT_REASON = "reason"
ALERT_CAT_ZONE = "zone"
ALERT_CAT_PAYLOAD = "payload"
ALERT_CAT_ETA = "eta"

# Which telemetry/calculated fields are eligible for filtering/saving.
STORE_DATA_TYPES = {
    STORE_RECV_DATA: [STORE_LAT, STORE_ALT, STORE_LONG, STORE_VERT_SPEED,
                      STORE_HORIZ_SPEED, STORE_HEADING],
    STORE_CALC_DATA: [STORE_HORIZ_SPEED, STORE_HEADING],
}

# Human-readable plane category lookup keyed by (typecode_category, category_code).
STORE_PLANE_CATEGORY_CONVERSION = {
    2: {1: "Surface Emergency Vehicle", 3: "Surface Service Vehicle",
        4: "Ground Obstruction (4)", 5: "Ground Obstruction (5)",
        6: "Ground Obstruction (6)", 7: "Ground Obstruction (7)"},
    3: {1: "Glider/Sailplane", 2: "Lighter-than-air", 3: "Parachutist/Skydiver",
        4: "Ultralight/Hang-glider/paraglider", 6: "UAV (unmanned aerial vehicle)",
        7: "Space/transatmospheric vehicle"},
    4: {1: "Light (<7000kg)", 2: "Medium 1 (7000 to 34000kg)",
        3: "Medium 2 (34000 to 136000kg)", 4: "High vortex aircraft",
        5: "Heavy (>13600kg)",
        6: "High performance (>5g) and high speed (>740km/h)",
        7: "Rotorcraft (helicopter)"},
}


# --- Component comparison operators -------------------------------------------
CONFIG_COMP_CTYPE_MINIMUM = "minimum"
CONFIG_COMP_CTYPE_MAXIMUM = "maximum"
CONFIG_COMP_FUNCTIONS = {
    CONFIG_COMP_CTYPE_MAXIMUM: operator.le,
    CONFIG_COMP_CTYPE_MINIMUM: operator.ge,
}

# --- Logging ------------------------------------------------------------------
LOGGING_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}

# --- Defaults -----------------------------------------------------------------
from pathlib import Path
DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_AIRCRAFT_DB = str((Path(__file__).resolve().parent / ".." / ".." / ".." / "aircraft.db").resolve())
