"""
ADS-B message classification (pyModeS).
"""
from __future__ import annotations

import logging

import pyModeS as pms

from pyaerial.constants import (
    CONFIG_HOME,
    CONFIG_HOME_LATITUDE,
    CONFIG_HOME_LONGITUDE,
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_LONG,
    STORE_LAT,
    STORE_RECV_DATA,
    STORE_PLANE_CATEGORY,
    STORE_CALLSIGN,
    STORE_VERT_SPEED,
)

log = logging.getLogger("pyaerial.decode")


def classify(msg: str, configuration: dict):
    """
    Classify an ADS-B message (downlink 17 or 18).
    :return: (data dict, typecode_category) or None if skipped
    """
    typecode = pms.typecode(msg)
    if typecode == -1:
        data = {STORE_INFO: {STORE_ICAO: pms.icao(msg)}, STORE_RECV_DATA: {}, STORE_CALC_DATA: {}}
        return data, 0
    data = None
    icao = pms.icao(msg)
    if len(icao) != 6 or icao == "000000":
        return None
    typecode_category = -1
    home = configuration[CONFIG_HOME]

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
        typecode_category = 1

    elif 5 <= typecode <= 8:
        lat, lon = pms.adsb.position_with_ref(
            msg, home[CONFIG_HOME_LATITUDE], home[CONFIG_HOME_LONGITUDE]
        )
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(
            msg, source=True
        )
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {
                STORE_LAT: lat,
                STORE_LONG: lon,
                STORE_HORIZ_SPEED: speed * 1.852,
                STORE_HEADING: angle,
            },
        }
        typecode_category = 2

    elif 9 <= typecode <= 18 or 20 <= typecode <= 22:
        lat, lon = pms.adsb.position_with_ref(
            msg, home[CONFIG_HOME_LATITUDE], home[CONFIG_HOME_LONGITUDE]
        )
        alt = pms.adsb.altitude(msg) * 0.3048
        data = {STORE_INFO: {STORE_ICAO: icao}, STORE_RECV_DATA: {STORE_LAT: lat, STORE_LONG: lon, STORE_ALT: alt}}
        typecode_category = 3 if 9 <= typecode <= 18 else 4

    elif typecode == 19:
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(
            msg, source=True
        )
        data = {
            STORE_INFO: {STORE_ICAO: icao},
            STORE_RECV_DATA: {
                STORE_HORIZ_SPEED: speed * 1.852,
                STORE_HEADING: angle,
                STORE_VERT_SPEED: vert_rate * 0.00508,
            },
        }
        typecode_category = 5

    elif typecode in (28, 29, 31):
        return None

    if data is None:
        log.warning("Received confusing typecode %s (msg=%s)", typecode, msg)
        return None

    log.debug("Collected ADS-B message from typecode %s: %s", typecode, data)
    data.update({STORE_CALC_DATA: {}})

    checked_data = data.copy()
    for message_type in data.keys():
        for subcategory in list(data[message_type].keys()):
            if data[message_type][subcategory] is None:
                del checked_data[message_type][subcategory]

    return checked_data, typecode_category
