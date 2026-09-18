"""Receiver streaming raw Mode S frames from dump1090 TCP sockets (AVR or Beast format)."""

from __future__ import annotations

import socket
import time

from pyaerial.receivers import Receiver, register_receiver
from pyaerial.receivers.frames import (
    BeastParser,
    _BEAST_BUF_MAX,
    parse_avr_line,
    receive_times,
)

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
            client = socket.create_connection((self.ip, self.port), timeout=_SOCKET_TIMEOUT)
        except OSError as exc:
            return f"failed to connect to {self.ip}:{self.port} ({exc})"
        client.settimeout(_SOCKET_TIMEOUT)
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
            try:
                client.close()
            except OSError:
                pass

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
            if len(buffer) > _BEAST_BUF_MAX:
                buffer = buffer[-1024:]
            lines = buffer.split("\n")
            buffer = lines.pop()
            parsed_rows: list[tuple[str, int | None]] = []
            for line in lines:
                parsed = parse_avr_line(line)
                if parsed:
                    parsed_rows.append(parsed)
            if not parsed_rows:
                continue
            now = time.time()
            stamps = receive_times(now, [clock for _hex, clock in parsed_rows])
            for (hex_msg, clock), stamp in zip(parsed_rows, stamps):
                self.emit(hex_msg, stamp, rssi=None, clock=clock)

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

            frames = parser.feed(chunk)
            if not frames:
                continue
            now = time.time()
            stamps = receive_times(now, [clock for _hex, _rssi, clock in frames])
            for (hex_msg, rssi, clock), stamp in zip(frames, stamps):
                self.emit(hex_msg, stamp, rssi=rssi, clock=clock)
        return None
