"""
ADS-B / Mode S message classification.

Turns a raw hex message into structured plane data (info + received_data) and a
typecode category used for internal bookkeeping.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pyModeS as pms

from pyaerial.config.schema import HomeConfig
from pyaerial.constants import (
    STORE_ALT,
    STORE_CALC_DATA,
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

log = logging.getLogger("pyaerial.classify")

# Internal packet-type buckets used for status reporting.
CAT_UNKNOWN = 0
CAT_IDENT = 1
CAT_SURFACE = 2
CAT_AIRBORNE_BARO = 3
CAT_AIRBORNE_GNSS = 4
CAT_VELOCITY = 5


@dataclass(frozen=True, slots=True)
class ClassifiedMessage:
    data: dict
    typecode_category: int


def classify(msg: str, home: HomeConfig) -> ClassifiedMessage | None:
    """
    Classify a single ADS-B message.

  Assumes downlink format 17 or 18. Returns ``None`` for messages that should
  be ignored (invalid ICAO, unsupported typecode, etc.).
    """
    typecode = pms.typecode(msg)

    if typecode == -1:
        icao = pms.icao(msg)
        if not _valid_icao(icao):
            return None
        return ClassifiedMessage(
            data={
                STORE_INFO: {STORE_ICAO: icao},
                STORE_RECV_DATA: {},
                STORE_CALC_DATA: {},
            },
            typecode_category=CAT_UNKNOWN,
        )

    icao = pms.icao(msg)
    if not _valid_icao(icao):
        return None

    data: dict | None = None
    category = CAT_UNKNOWN

    if 1 <= typecode <= 4:
        ca = pms.adsb.category(msg)
        data = {
            STORE_INFO: {
                STORE_ICAO: icao,
                STORE_CALLSIGN: pms.adsb.callsign(msg).replace("_", ""),
                STORE_PLANE_CATEGORY: [typecode, ca],
            },
            STORE_RECV_DATA: {},
        }
        category = CAT_IDENT

    elif 5 <= typecode <= 8:
        lat, lon = pms.adsb.position_with_ref(msg, home.latitude, home.longitude)
        speed, angle, _, _, _, _ = pms.adsb.velocity(msg, source=True)
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {
                STORE_LAT: lat,
                STORE_LONG: lon,
                STORE_HORIZ_SPEED: speed * 1.852,
                STORE_HEADING: angle,
            },
        }
        category = CAT_SURFACE

    elif 9 <= typecode <= 18 or 20 <= typecode <= 22:
        lat, lon = pms.adsb.position_with_ref(msg, home.latitude, home.longitude)
        alt = pms.adsb.altitude(msg) * 0.3048
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {STORE_LAT: lat, STORE_LONG: lon, STORE_ALT: alt},
        }
        category = CAT_AIRBORNE_BARO if typecode <= 18 else CAT_AIRBORNE_GNSS

    elif typecode == 19:
        speed, angle, vert_rate, _, _, _ = pms.adsb.velocity(msg, source=True)
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {
                STORE_HORIZ_SPEED: speed * 1.852,
                STORE_HEADING: angle,
                STORE_VERT_SPEED: vert_rate * 0.00508,
            },
        }
        category = CAT_VELOCITY

    elif typecode in (28, 29, 31):
        return None

    if data is None:
        log.warning("Unsupported typecode %s (msg=%s)", typecode, msg)
        return None

    data[STORE_CALC_DATA] = {}
    data = _strip_nulls(data)
    log.debug("Classified typecode %s: %s", typecode, data)
    return ClassifiedMessage(data=data, typecode_category=category)


def _valid_icao(icao: str) -> bool:
    return len(icao) == 6 and icao != "000000"


def _strip_nulls(data: dict) -> dict:
    cleaned = {}
    for bucket, fields in data.items():
        if not isinstance(fields, dict):
            cleaned[bucket] = fields
            continue
        kept = {k: v for k, v in fields.items() if v is not None}
        cleaned[bucket] = kept
    return cleaned
