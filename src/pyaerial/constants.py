"""
Domain constants shared across PyAerial modules.

These are stable identifiers for data fields, message categories, and database
keys. Anything that is user-configurable now lives in the typed configuration
schema (:mod:`pyaerial.config`) rather than here.
"""

import operator
from pathlib import Path

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
DEFAULT_CONFIG_FILE = "config.yaml"
DEFAULT_AIRCRAFT_DB = str(
    (Path(__file__).resolve().parent / ".." / ".." / ".." / "aircraft.db").resolve()
)
