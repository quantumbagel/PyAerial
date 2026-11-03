"""Receiver streaming raw Mode S frames from dump1090 TCP sockets (AVR or Beast format)."""

from __future__ import annotations

import contextlib
import socket
import time
from pathlib import Path
from typing import Any

from pyaerial.receivers import Receiver, ReceiverEndpoint, register_receiver
from pyaerial.receivers.frames import (
    _BEAST_BUF_MAX,
    BeastParser,
    parse_avr_line,
    receive_times,
)

_RECV_BUFFER = 65_536
_SOCKET_TIMEOUT = 1.0
# dump1090-fa and readsb send a heartbeat at least every 60 s on idle feeds. We
# never write to the socket, so a feeder that rebooted (half-open connection)
# is only noticed by silence; reconnecting then is cheap.
_IDLE_TIMEOUT = 150.0
_AVR_FORMATS = {"avr", "raw"}
_BEAST_FORMATS = {"beast", "binary"}
_AVR_PORT = 30002
_BEAST_PORT = 30005


@register_receiver("dump1090")
class Dump1090Receiver(Receiver):
    # tcp_connection_* also come from the top-level host/port fields.
    known_options = frozenset({"format", "tcp_connection_ip", "tcp_connection_port"})

    @classmethod
    def validate_options(cls, options: dict[str, Any], *, config_dir: Path) -> list[str]:
        raw_fmt = options.get("format")
        fmt = str(raw_fmt).lower() if raw_fmt is not None else None
        if fmt is not None and fmt not in _AVR_FORMATS | _BEAST_FORMATS:
            # configure() would fall back to AVR with only a log warning.
            return [f"format {raw_fmt!r} is not one of avr, raw, beast, binary"]
        try:
            port = int(options["tcp_connection_port"])
        except (KeyError, TypeError, ValueError):
            port = None
        if port == _AVR_PORT and fmt in _BEAST_FORMATS:
            return [
                f"format {fmt} is Beast binary; port 30002 is AVR text. "
                "Use port 30005, or format: avr."
            ]
        if port == _BEAST_PORT and fmt in _AVR_FORMATS:
            return [
                f"format {fmt} is AVR text; port 30005 is Beast binary. "
                "Use port 30002, or format: beast."
            ]
        return []

    @classmethod
    def endpoint(cls, options: dict[str, Any]) -> ReceiverEndpoint:
        """Host, port and wire format (``beast`` or ``avr``) for ``options``.

        The one place the format is decided: an explicit ``format`` wins (an
        unknown name means AVR); otherwise port 30002 means AVR, anything else
        Beast, which carries the 12 MHz clock and signal level the integrity
        checks need. Raises ValueError for a non-numeric port.
        """
        raw_fmt = options.get("format")
        raw_port = options.get("tcp_connection_port")
        port = int(raw_port) if raw_port is not None else None
        if raw_fmt is not None:
            wire = "beast" if str(raw_fmt).lower() in _BEAST_FORMATS else "avr"
        else:
            wire = "avr" if port == _AVR_PORT else "beast"
        if port is None:
            port = _BEAST_PORT if wire == "beast" else _AVR_PORT
        host = str(options.get("tcp_connection_ip") or "localhost")
        return ReceiverEndpoint(host, port, wire)

    def configure(self, arguments: dict) -> None:
        endpoint = self.endpoint(arguments)
        fmt_raw = arguments.get("format")
        if fmt_raw is not None and str(fmt_raw).lower() not in _AVR_FORMATS | _BEAST_FORMATS:
            self.log.warning(
                "Unknown dump1090 format %r; using AVR. Valid: avr, beast",
                str(fmt_raw).lower(),
            )
        self.ip = str(arguments.get("tcp_connection_ip", "localhost"))
        self.port = endpoint.port
        self.format = endpoint.wire_format

    def run(self) -> str | None:
        try:
            client = socket.create_connection(
                (self.ip, self.port), timeout=_SOCKET_TIMEOUT
            )
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
            with contextlib.suppress(OSError):
                client.close()

    def _recv(self, client: socket.socket, last_data: float) -> bytes | str | None:
        """One ``recv``: bytes, ``None`` on a quiet second, or a reason to stop."""
        try:
            chunk = client.recv(_RECV_BUFFER)
        except TimeoutError:
            if time.monotonic() - last_data > _IDLE_TIMEOUT:
                return f"no data for {_IDLE_TIMEOUT:.0f}s"
            return None
        except ConnectionResetError:
            return "connection reset by peer"
        except OSError as exc:
            return f"socket error ({exc})"
        if not chunk:
            return "socket connection closed by peer"
        return chunk

    def _run_avr(self, client: socket.socket) -> str | None:
        buffer = ""
        last_data = time.monotonic()
        while not self.should_stop():
            chunk = self._recv(client, last_data)
            if chunk is None:
                continue
            if isinstance(chunk, str):
                return chunk
            last_data = time.monotonic()

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
            for (hex_msg, clock), stamp in zip(parsed_rows, stamps, strict=True):
                self.emit(hex_msg, stamp, rssi=None, clock=clock)

        return None

    def _run_beast(self, client: socket.socket) -> str | None:
        parser = BeastParser()
        last_data = time.monotonic()
        while not self.should_stop():
            chunk = self._recv(client, last_data)
            if chunk is None:
                continue
            if isinstance(chunk, str):
                return chunk
            last_data = time.monotonic()

            frames = parser.feed(chunk)
            if not frames:
                continue
            now = time.time()
            stamps = receive_times(now, [clock for _hex, _rssi, clock in frames])
            for (hex_msg, rssi, clock), stamp in zip(frames, stamps, strict=True):
                self.emit(hex_msg, stamp, rssi=rssi, clock=clock)
        return None
