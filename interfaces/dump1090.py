"""
Another interface that PyAerial can use that uses dump1090's networking to stream packets
"""

import socket
import time
import logging

message_queue = []


def run():
    log = logging.getLogger("dump1090_interface")
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect((socket.gethostbyname("127.0.0.1"), 30002))
    except ConnectionRefusedError as e:
        log.fatal("Failed to connect to TCP stream")
        raise e
    while True:
        data = client.recv(1024).decode('utf-8')[1:-2]
        for item in data.split("\n"):
            message_queue.append([item.replace("*", "").replace(";", ""), time.time()])
