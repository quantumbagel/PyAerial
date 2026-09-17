"""
ADS-B / Mode S message classification.

Turns a raw hex message into structured plane data (info + received_data) and a
typecode category used for internal bookkeeping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pyModeS as pms
from pyModeS.util import typecode as pms_typecode, icao as pms_icao

from pyaerial.config.schema import HomeConfig
from pyaerial.constants import (
    STORE_ALT,
    STORE_CALLSIGN,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_LAT,
    STORE_LONG,
    STORE_PLANE_CATEGORY,
    STORE_RECV_DATA,
    STORE_VERT_SPEED,
)
from pyaerial.units import FT_PER_MIN_TO_MPS, FT_TO_M, KT_TO_KMH

_MAX_CPR_JUMP_DEG = 0.5

log = logging.getLogger("pyaerial.classify")

# Internal packet-type buckets used for status reporting.
CAT_IDENT = 1
CAT_SURFACE = 2
CAT_AIRBORNE_BARO = 3
CAT_AIRBORNE_GNSS = 4
CAT_VELOCITY = 5


@dataclass(frozen=True, slots=True)
class ClassifiedMessage:
    data: dict
    typecode_category: int


def classify(
    msg: str,
    home: HomeConfig,
    last_position: tuple[float, float] | None = None,
) -> ClassifiedMessage | None:
    """
    Classify a single ADS-B message.

    Assumes downlink format 17 or 18. Returns ``None`` for messages that should
    be ignored (invalid ICAO, unsupported typecode, etc.).
    """
    try:
        typecode = pms_typecode(msg)
    except Exception:
        return None

    # pyModeS v2 used -1; v3 returns None for non-DF17/18 (short Mode S, DF11).
    if typecode is None or typecode == -1:
        return None

    try:
        icao = pms_icao(msg)
    except Exception:
        return None
    if not _valid_icao(icao):
        return None

    data: dict | None = None
    category: int | None = None

    if 1 <= typecode <= 4:
        try:
            decoded = pms.decode(msg)
        except Exception:
            return None
        ca = decoded.get("category")
        callsign = decoded.get("callsign") or ""
        callsign = callsign.replace("_", "").strip()
        info = {
            STORE_ICAO: icao,
            STORE_PLANE_CATEGORY: [typecode, ca],
        }
        if callsign:
            info[STORE_CALLSIGN] = callsign
        data = {
            STORE_INFO: info,
            STORE_RECV_DATA: {},
        }
        category = CAT_IDENT

    elif 5 <= typecode <= 8:
        ref = last_position or (home.latitude, home.longitude)
        try:
            decoded = pms.decode(msg, surface_ref=ref)
        except Exception:
            return None
        lat = decoded.get("latitude")
        lon = decoded.get("longitude")
        if not _plausible_fix(lat, lon, last_position):
            return None
        speed = decoded.get("groundspeed")
        angle = decoded.get("track")
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {
                STORE_LAT: lat,
                STORE_LONG: lon,
                STORE_HORIZ_SPEED: speed * KT_TO_KMH if speed is not None else None,
                STORE_HEADING: angle,
            },
        }
        category = CAT_SURFACE

    elif 9 <= typecode <= 18 or 20 <= typecode <= 22:
        ref = last_position or (home.latitude, home.longitude)
        try:
            decoded = pms.decode(msg, reference=ref)
        except Exception:
            return None
        lat = decoded.get("latitude")
        lon = decoded.get("longitude")
        if not _plausible_fix(lat, lon, last_position):
            return None
        alt = decoded.get("altitude")
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {
                STORE_LAT: lat,
                STORE_LONG: lon,
                STORE_ALT: alt * FT_TO_M if alt is not None else None,
            },
        }
        category = CAT_AIRBORNE_BARO if typecode <= 18 else CAT_AIRBORNE_GNSS

    elif typecode == 19:
        try:
            decoded = pms.decode(msg)
        except Exception:
            return None
        speed = decoded.get("groundspeed")
        angle = decoded.get("track")
        vert_rate = decoded.get("vertical_rate")
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {
                STORE_HORIZ_SPEED: speed * KT_TO_KMH if speed is not None else None,
                STORE_HEADING: angle,
                STORE_VERT_SPEED: vert_rate * FT_PER_MIN_TO_MPS
                if vert_rate is not None
                else None,
            },
        }
        category = CAT_VELOCITY

    elif typecode in (28, 29, 31):
        return None

    if data is None or category is None:
        log.debug("Unsupported typecode %s (msg=%s)", typecode, msg)
        return None

    data = _strip_nulls(data)
    log.debug("Classified typecode %s: %s", typecode, data)
    return ClassifiedMessage(data=data, typecode_category=category)


def _valid_icao(icao: object) -> bool:
    return isinstance(icao, str) and len(icao) == 6 and icao != "000000"


def _plausible_fix(
    lat: object,
    lon: object,
    last_position: tuple[float, float] | None,
) -> bool:
    if lat is None or lon is None:
        return True
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    if last_position is None:
        return True
    return (
        abs(float(lat) - last_position[0]) <= _MAX_CPR_JUMP_DEG
        and abs(float(lon) - last_position[1]) <= _MAX_CPR_JUMP_DEG
    )


def _strip_nulls(data: dict) -> dict:
    cleaned = {}
    for bucket, fields in data.items():
        if not isinstance(fields, dict):
            cleaned[bucket] = fields
            continue
        kept = {k: v for k, v in fields.items() if v is not None}
        cleaned[bucket] = kept
    return cleaned
