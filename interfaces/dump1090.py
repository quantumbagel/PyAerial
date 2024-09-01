"""
Another interface that PyAerial can use that uses dump1090's networking to stream packets
"""

import time
import socket
from constants import *

def run(pipeline: dict, ip: str, port: str = "30002"):
    """
    Run the interface.

    :param pipeline: Dictionary of pipeline information as specified by PyAerial
    :param ip: IP address of the TCP server to connect to
    :param port: Port of the TCP server to connect to

    Will update the pipeline variable with information it generates.
    """
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect((socket.gethostbyname(ip), int(port)))
    except ConnectionRefusedError:
        pipeline[STORE_PIPELINE_LAST_RETURN] = "Failed to connect to TCP stream"
        return
    while True:
        try:

            data = client.recv(1024).decode('utf-8').replace("*", "").replace(";", "")
            if len(data) == 0:
                pipeline[STORE_PIPELINE_LAST_RETURN] = "Socket connection has wedged."
                client.close()
                return
        except ConnectionResetError:
            continue
        for item in data.split("\n"):
            if item:
                (pipeline[STORE_PIPELINE_MESSAGES]
                 .append([item.replace("*", "").replace(";", ""), time.time()]))
