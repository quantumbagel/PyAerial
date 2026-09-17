"""
Receiver that streams raw messages from dump1090's TCP output.

Run ``dump1090 --net --raw`` (or broadcast raw messages over TCP) and point this
receiver at the host/port.

AVR text (default, port 30002) carries hex frames. Beast binary (port 30005,
``options.format: beast``) also carries per-message RSSI (dBFS) and a
12 MHz-tick clock.
"""

from __future__ import annotations

import socket
import time

from pyaerial.receivers import Receiver, register_receiver
from pyaerial.receivers.frames import BeastParser, parse_avr_line

_RECV_BUFFER = 4096
_SOCKET_TIMEOUT = 1.0
_AVR_FORMATS = {"avr", "raw"}
_BEAST_FORMATS = {"beast", "binary"}


@register_receiver("dump1090")
class Dump1090Receiver(Receiver):
    def configure(self, arguments: dict) -> None:
        self.ip = str(arguments.get("tcp_connection_ip", "localhost"))
        port = arguments.get("tcp_connection_port")
        fmt_raw = arguments.get("format")
        if fmt_raw is not None:
            fmt = str(fmt_raw).lower()
            if fmt in _BEAST_FORMATS:
                self.format = "beast"
            elif fmt in _AVR_FORMATS:
                self.format = "avr"
            else:
                self.format = "avr"
                self.log.warning(
                    "Unknown dump1090 format %r; using AVR. Valid: avr, beast",
                    fmt,
                )
        elif port is not None and int(port) == 30005:
            self.format = "beast"
        else:
            self.format = "avr"
        if port is not None:
            self.port = int(port)
        else:
            self.port = 30005 if self.format == "beast" else 30002

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

        self.log.info(
            "Connected to dump1090 %s stream at %s:%s",
            self.format,
            self.ip,
            self.port,
        )
        try:
            if self.format == "beast":
                return self._run_beast(client)
            return self._run_avr(client)
        finally:
            client.close()

    def _run_avr(self, client: socket.socket) -> str | None:
        buffer = ""
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
            buffer = lines.pop()
            now = time.time()
            for line in lines:
                parsed = parse_avr_line(line)
                if not parsed:
                    continue
                hex_msg, clock = parsed
                self.emit(hex_msg, now, rssi=None, clock=clock)

        return None

    def _run_beast(self, client: socket.socket) -> str | None:
        parser = BeastParser()
        while not self.should_stop():
            try:
                chunk = client.recv(_RECV_BUFFER)
            except socket.timeout:
                continue
            except ConnectionResetError:
                return "connection reset by peer"
            if not chunk:
                return "socket connection closed by peer"

            now = time.time()
            for hex_msg, rssi, clock in parser.feed(chunk):
                self.emit(hex_msg, now, rssi=rssi, clock=clock)
        return None
