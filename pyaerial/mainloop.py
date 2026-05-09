"""
Main tracking loop: ingest messages, update plane state, calculate, persist.
"""
from __future__ import annotations

import logging
import sys
import time
import types

from pyaerial import calculations, decode, receivers, rosetta
from pyaerial.constants import (
    CONFIG_GENERAL,
    CONFIG_GENERAL_ADVANCED_STATUS,
    CONFIG_GENERAL_HERTZ,
    CONFIG_GENERAL_LOGGING_LEVEL,
    CONFIG_GENERAL_MONGODB,
    CONFIG_GENERAL_REMEMBER,
    CONFIG_GENERAL_TOP_PLANES,
    LOGGING_LEVELS,
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_MOST_RECENT_PACKET,
    STORE_TOTAL_PACKETS,
    STORE_PACKET_TYPE,
    STORE_RECV_DATA,
    STORE_CALLSIGN,
)
from pyaerial.helpers import Datum

log = logging.getLogger("pyaerial")

planes: dict = {}


def check_should_be_added(packets, new_packet):
    return packets[-1].value != new_packet.value


def process_messages(msgs: list, configuration: dict) -> int:
    processed = 0
    for message in msgs:
        try:
            result = decode.classify(message[0], configuration)
            if result is None:
                continue
            message_data, typecode_cat = result
        except Exception:
            continue
        processed += 1
        icao = message_data[STORE_INFO][STORE_ICAO]

        if icao not in planes:
            planes[icao] = message_data
            for item in planes[icao][STORE_RECV_DATA]:
                c_item = planes[icao][STORE_RECV_DATA][item]
                planes[icao][STORE_RECV_DATA][item] = [Datum(c_item, message[1])]
        else:
            current_info = planes[icao][STORE_INFO]
            my_info = message_data[STORE_INFO]
            for item in my_info:
                current_info[item] = my_info[item]
            planes[icao][STORE_INFO] = current_info
            current_data = planes[icao][STORE_RECV_DATA]
            for datum_key in message_data[STORE_RECV_DATA]:
                new_packet = Datum(message_data[STORE_RECV_DATA][datum_key], message[1])
                if datum_key not in current_data:
                    current_data[datum_key] = [new_packet]
                elif check_should_be_added(current_data[datum_key], new_packet):
                    current_data[datum_key].append(new_packet)

        if STORE_INTERNAL not in planes[icao]:
            planes[icao][STORE_INTERNAL] = {
                STORE_MOST_RECENT_PACKET: message[1],
                STORE_TOTAL_PACKETS: 1,
                STORE_FIRST_PACKET: message[1],
                STORE_PACKET_TYPE: {typecode_cat: 1},
            }
        else:
            internal_data_storage = planes[icao][STORE_INTERNAL]
            internal_data_storage[STORE_MOST_RECENT_PACKET] = message[1]
            internal_data_storage[STORE_TOTAL_PACKETS] += 1
            if typecode_cat in internal_data_storage[STORE_PACKET_TYPE]:
                internal_data_storage[STORE_PACKET_TYPE][typecode_cat] += 1
            else:
                internal_data_storage[STORE_PACKET_TYPE][typecode_cat] = 1
    return processed


def calculate() -> None:
    for plane in planes:
        calculations.calculate_plane(planes[plane])


def check_for_old_planes(current_time: float, configuration: dict) -> list:
    old_planes = []
    remember = configuration[CONFIG_GENERAL][CONFIG_GENERAL_REMEMBER]
    for plane in planes:
        last_packet_relative_time_ago = current_time - planes[plane][STORE_INTERNAL][STORE_MOST_RECENT_PACKET]
        if last_packet_relative_time_ago > remember:
            old_planes.append(plane)
    return old_planes


def process_old_planes(old_planes: list, defined_saver: rosetta.Saver) -> None:
    plog = log.getChild("process_old_planes")
    should_run = False
    for plane in old_planes:
        plog.debug('Caching plane "%s"', plane)
        if defined_saver.cache_flight(planes[plane]):
            should_run = True
        del planes[plane]
    if should_run:
        plog.critical("Flushed planes: %s", old_planes)
        defined_saver.save()


def get_top_planes(current_planes: dict, top: int | None = None, advanced: bool = False) -> str:
    planes_by_packets = {
        p: current_planes[p][STORE_INTERNAL][STORE_TOTAL_PACKETS] for p in current_planes
    }
    sorted_planes = dict(sorted(planes_by_packets.items(), key=lambda item: item[1], reverse=True))
    message = ""
    if top:
        for current_number, plane in enumerate(sorted_planes):
            if current_number + 1 == top and top != -1:
                break
            if not advanced:
                message += f"{plane} ({sorted_planes[plane]}), "
            else:
                if STORE_CALLSIGN in current_planes[plane][STORE_INFO] and current_planes[plane][STORE_INFO][
                    STORE_CALLSIGN
                ]:
                    message += (
                        f"{plane}/{current_planes[plane][STORE_INFO][STORE_CALLSIGN]} "
                        f"({sorted_planes[plane]}, {current_planes[plane][STORE_INTERNAL][STORE_PACKET_TYPE]}), "
                    )
                else:
                    message += (
                        f"{plane}, ({sorted_planes[plane]}, "
                        f"{current_planes[plane][STORE_INTERNAL][STORE_PACKET_TYPE]}), "
                    )
    if not message:
        return ""
    if top == -1:
        return message[:-2]
    if top > len(sorted_planes):
        top = len(sorted_planes)
    return f"Top {top}: " + message[:-2]


def run_forever(configuration: dict) -> None:
    saver = rosetta.MongoSaver(configuration[CONFIG_GENERAL][CONFIG_GENERAL_MONGODB])
    top_planes_n = configuration[CONFIG_GENERAL][CONFIG_GENERAL_TOP_PLANES]
    hz = configuration[CONFIG_GENERAL][CONFIG_GENERAL_HERTZ]

    log.info("PyAerial main loop starting.")
    try:
        while True:
            start_time = time.time()
            status = ""
            receiver_data = receivers.check_receivers()
            messages = receivers.get_new_messages(receiver_data, configuration)
            process_messages(messages, configuration)
            receivers.reset_message_queue()
            calculate()
            old = check_for_old_planes(time.time(), configuration)

            log.info(
                "%sTracking %s planes. %s",
                status,
                len(planes),
                get_top_planes(
                    planes,
                    top_planes_n,
                    configuration[CONFIG_GENERAL][CONFIG_GENERAL_ADVANCED_STATUS],
                ),
            )

            process_old_planes(old, saver)
            end_time = time.time()
            delta = 1 / hz - (end_time - start_time)
            if delta > 0:
                try:
                    time.sleep(delta)
                except KeyboardInterrupt:
                    log.critical("Quitting (keyboard interrupt)")
                    sys.exit(0)
            else:
                log.warning(
                    "Main loop behind by %s s (%s/%s)",
                    round(-delta, 2),
                    round(end_time - start_time, 2),
                    1 / hz,
                )
    except Exception as e:
        log.critical("PyAerial critical failure; dumping selected globals")
        variables = globals()
        for name in variables:
            if name.startswith("__"):
                continue
            val = variables[name]
            if isinstance(val, (types.ModuleType, types.BuiltinFunctionType, types.FunctionType)):
                continue
            log.critical("%s: %s", name, val)
        raise e
