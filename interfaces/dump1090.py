"""
Another interface that PyAerial can use that uses dump1090's networking to stream packets
"""

import time
import logging
import socket

message_queue = []  # Stores (message, time), intercepted by the PyAerial main module


def run():
    log = logging.getLogger("dump1090_interface")
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect((socket.gethostbyname("127.0.0.1"), 30002))
    except ConnectionRefusedError:
        log.fatal("Failed to connect to TCP stream")
        return
    while True:
        try:

            data = client.recv(1024).decode('utf-8').replace("*", "").replace(";", "")
            if len(data) == 0:
                log.fatal("Socket connection has wedged. Going to exit now.")
                client.close()
                return
        except ConnectionResetError:
            log.fatal("Connection reset by peer!")
            continue
        for item in data.split("\n"):
            message_queue.append([item.replace("*", "").replace(";", ""), time.time()])
