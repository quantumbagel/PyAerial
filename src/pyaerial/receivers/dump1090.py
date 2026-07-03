"""
Receiver that streams raw messages from dump1090's TCP output.

Run ``dump1090 --net --raw`` (or broadcast raw messages over TCP) and point this
receiver at the host/port.
"""
from __future__ import annotations

import socket
import time

from pyaerial.receivers import Receiver, register_receiver

_RECV_BUFFER = 4096
_SOCKET_TIMEOUT = 1.0


@register_receiver("dump1090")
class Dump1090Receiver(Receiver):
    def configure(self, arguments: dict) -> None:
        self.ip = str(arguments.get("tcp_connection_ip", "localhost"))
        self.port = int(arguments.get("tcp_connection_port", 30002))

    def run(self) -> str | None:
        try:
            resolved = socket.gethostbyname(self.ip)
        except socket.gaierror as exc:
            return f"could not resolve host {self.ip!r}: {exc}"

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(_SOCKET_TIMEOUT)
        try:
            client.connect((resolved, self.port))
        except (ConnectionRefusedError, OSError) as exc:
            return f"failed to connect to {self.ip}:{self.port} ({exc})"

        self.log.info("Connected to dump1090 stream at %s:%s", self.ip, self.port)
        buffer = ""
        try:
            while not self.should_stop():
                try:
                    chunk = client.recv(_RECV_BUFFER)
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    return "connection reset by peer"
                if not chunk:
                    return "socket connection closed by peer"

                buffer += chunk.decode("utf-8", errors="ignore")
                lines = buffer.split("\n")
                buffer = lines.pop()  # keep any partial line for next read
                now = time.time()
                for line in lines:
                    message = line.strip().replace("*", "").replace(";", "")
                    if message:
                        self.emit(message, now)
        finally:
            client.close()
        return None
