"""
Domain constants shared across PyAerial modules.

These are stable identifiers for data fields, message categories, and database
keys, plus shared kinematics limits. Anything that is user-configurable now
lives in the typed configuration schema (:mod:`pyaerial.config`) rather than here.
"""

import operator
import os
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
DEFAULT_CONFIG_FILE = os.environ.get("PYAERIAL_CONFIG", "config.yaml")
# Redis `live:engine` heartbeat TTL. Portal treats a missing/expired key as
# "tracking engine is not running." Keep in sync with web empty-state stale age.
LIVE_ENGINE_TTL_SECONDS = 10
# Local HexDB/Planespotters cache. Override with PYAERIAL_AIRCRAFT_DB or --aircraft-db.
# Relative paths resolve against the process working directory, not the package layout.
DEFAULT_AIRCRAFT_DB = os.environ.get(
    "PYAERIAL_AIRCRAFT_DB", str(Path.cwd() / "aircraft.db")
)
# Dead-reckon alerts only while the last position is this fresh (seconds).
# Ident/velocity packets keep the plane in memory for remember_planes, but
# coasting that whole window produces false geofence hits.
MAX_COAST_SECONDS = 5.0

# --- Kinematics ---------------------------------------------------------------
# Mid-latitude meters per degree of latitude (flat-earth local frame).
METERS_PER_DEG_LAT = 111_000.0
MIN_METERS_PER_DEG_LON = 1000.0
MIN_SPEED_DT = 0.2
MIN_VEL_DT = 0.05
MAX_KALMAN_DT = 30.0
# Unphysical ground-speed cap (~3000 km/h); rejects CPR/decode jumps.
MAX_SPEED_MPS = 833.0
MAX_CPR_JUMP_DEG = 0.5

# Names accepted in a zone rule's ``when`` block (canonical + aliases).
WHEN_FIELDS = frozenset(
    {
        STORE_HORIZ_SPEED,
        "horizontal_speed",
        STORE_HEADING,
        "direction",
        STORE_ALT,
        "alt",
        STORE_VERT_SPEED,
        "vert_speed",
        STORE_LAT,
        "lat",
        STORE_LONG,
        "lon",
        "long",
        STORE_DISTANCE,
        "dist",
        "proximity",
        ALERT_CAT_ETA,
    }
)
# At least one of these is required; there is no implicit inside-polygon test.
WHEN_SPATIAL_FIELDS = frozenset(
    {
        STORE_DISTANCE,
        "dist",
        "proximity",
        ALERT_CAT_ETA,
    }
)
