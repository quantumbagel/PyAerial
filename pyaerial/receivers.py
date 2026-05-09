"""
Multi-receiver ADS-B pipeline: dynamic interface loading and message batching.
"""
from __future__ import annotations

import importlib
import logging
import sys
import threading
from typing import Any

from pyaerial.constants import (
    CONFIG_GENERAL,
    CONFIG_GENERAL_MERGE_PACKETS,
    CONFIG_RECV_ARGUMENTS,
    CONFIG_RECV_METHOD,
    CONFIG_RECV_METHODS,
    CONFIG_RECEIVERS,
    INTERFACES_PACKAGE,
    STORE_PIPELINE_LAST_RETURN,
    STORE_PIPELINE_MESSAGES,
)

log = logging.getLogger("pyaerial.receivers")

interfaces: dict[str, Any] = {}
receivers: dict[str, list] = {}
recent_messages: list = []


def load_interfaces(configuration: dict) -> None:
    """Start receiver threads from configuration."""
    log_load = logging.getLogger("pyaerial.load_interfaces")
    configuration_receivers = configuration[CONFIG_RECEIVERS]
    for receiver_name in configuration_receivers:
        receiver = configuration_receivers[receiver_name]
        method = receiver[CONFIG_RECV_METHOD]
        if method not in interfaces:
            try:
                interfaces[method] = importlib.import_module(f"{INTERFACES_PACKAGE}.{method}")
            except ModuleNotFoundError:
                log_load.error(
                    "Failed to load module %s.interfaces.%s (receiver %s). Skipping.",
                    INTERFACES_PACKAGE.rsplit(".", 1)[0],
                    method,
                    receiver_name,
                )
                continue
            pipeline = {STORE_PIPELINE_LAST_RETURN: "", STORE_PIPELINE_MESSAGES: []}
            arguments = [pipeline]
            for argument in CONFIG_RECV_METHODS[method].keys():
                arguments.append(receiver[CONFIG_RECV_ARGUMENTS][argument])

            receiver_thread = threading.Thread(target=interfaces[method].run, args=arguments)
            receiver_thread.start()
            receivers[receiver_name] = [receiver_thread, arguments, method, pipeline]
    if not receivers:
        log_load.error("No valid receivers in configuration. Exiting.")
        sys.exit(0)


def check_receivers() -> list:
    """Restart dead receiver threads; return message lists from each pipeline."""
    log_chk = log.getChild("check_receivers")
    for receiver_name in receivers:
        receiver_data = receivers[receiver_name]
        thread = receiver_data[0]
        arguments = receiver_data[1]
        packet_method = receiver_data[2]
        pipeline = receiver_data[3]

        if not thread.is_alive():
            if not pipeline[STORE_PIPELINE_LAST_RETURN]:
                pipeline[STORE_PIPELINE_LAST_RETURN] = "Crashed unexpectedly (unhandled exception)"

            log_chk.warning(
                'Receiver "%s" of type "%s" died with error "%s". Restarting...',
                receiver_name,
                packet_method,
                pipeline[STORE_PIPELINE_LAST_RETURN],
            )

            receiver_thread = threading.Thread(target=interfaces[packet_method].run, args=arguments)
            receiver_thread.start()
            receivers[receiver_name][0] = receiver_thread
            pipeline[STORE_PIPELINE_LAST_RETURN] = ""

    return [receivers[r][3][STORE_PIPELINE_MESSAGES] for r in receivers]


def get_new_messages(receiver_data: list, configuration: dict) -> list:
    """
    Deduplicate and merge messages from receivers using duplicate_packet_merging window.
    """
    log_g = log.getChild("get_new_messages")
    global recent_messages

    merge_window = configuration[CONFIG_GENERAL][CONFIG_GENERAL_MERGE_PACKETS]
    current_timestamp = max([max([j[1] for j in i]) for i in receiver_data if len(i)], default=-1)

    if current_timestamp == -1:
        log_g.debug("There are no new messages to parse. Returning empty list.")
        return []

    unsorted_flattened_messages = []
    recent_unique_messages = [i[0] for i in recent_messages]
    for receiver in receiver_data:
        unsorted_flattened_messages.extend(receiver)

    def sort_by_second_item(item) -> float:
        return item[1]

    flattened_messages = sorted(unsorted_flattened_messages, key=sort_by_second_item)
    to_add = []

    for message in flattened_messages:
        if message[0] not in recent_unique_messages:
            to_add.append(message)
            recent_unique_messages.append(message[0])
            recent_messages.append(message)

        else:
            for potential_match in recent_messages:
                if potential_match[0] == message[0] and abs(potential_match[1] - message[1]) > merge_window:
                    to_add.append(potential_match)
                    recent_messages.append(message)
                    break
                elif potential_match[0] == message[0]:
                    break

    new_recent_messages = []
    for recent in recent_messages:
        if current_timestamp - recent[1] < merge_window:
            new_recent_messages.append(recent)
    recent_messages = new_recent_messages[:]

    return to_add


def reset_message_queue() -> None:
    for receiver_name in receivers:
        receivers[receiver_name][3][STORE_PIPELINE_MESSAGES] = []
