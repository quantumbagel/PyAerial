"""Receiver connections: dump1090 and AoA sockets (errors, silence, clocks)
and the local receive time stamped on their frames."""

from __future__ import annotations

import errno
import json
import socket
import threading
import time

from helpers import (
    DF17_HEX,
    IDENT,
)
from pyaerial.receivers import aoa, create_receiver, dump1090
from pyaerial.receivers.frames import encode_beast

# --- Receiver sockets ------------------------------------------------------------


class _FakeSocket:
    def __init__(self, error: BaseException | None = None):
        self.error = error
        self.closed = False

    def settimeout(self, _value):
        pass

    def recv(self, _size):
        if self.error is not None:
            raise self.error
        raise TimeoutError

    def close(self):
        self.closed = True


def test_dump1090_socket_error_ends_run_with_reason(monkeypatch):
    fake = _FakeSocket(OSError(errno.EHOSTUNREACH, "No route to host"))
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: fake)
    receiver = dump1090.Dump1090Receiver("main", lambda *_a, **_k: None, {})
    reason = receiver.run()
    assert reason and "No route to host" in reason
    assert fake.closed


def test_dump1090_silent_connection_is_dropped(monkeypatch):
    """A half-open TCP connection (feeder rebooted) never errors on recv; the
    receiver must give up so the engine reconnects."""
    fake = _FakeSocket()
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: fake)
    monkeypatch.setattr(dump1090, "_IDLE_TIMEOUT", 0.05)
    receiver = dump1090.Dump1090Receiver("main", lambda *_a, **_k: None, {})
    done = threading.Event()
    result: list[str | None] = []

    def target():
        result.append(receiver.run())
        done.set()

    threading.Thread(target=target, daemon=True).start()
    assert done.wait(2.0)
    receiver.stop()
    assert result[0] and "no data" in result[0]


def test_aoa_clock_must_be_a_48_bit_integer():
    def clock_of(value: str):
        line = '{"hex": "%s", "clock": %s}' % (IDENT, value)
        return aoa.parse_aoa_line(line)["clock"]

    assert clock_of("123") == 123
    assert clock_of("true") is None
    assert clock_of("-1") is None
    assert clock_of(str(1 << 48)) is None
    assert clock_of("1.5") is None


def test_aoa_socket_error_and_silence_end_run_with_reason(monkeypatch):
    fake = _FakeSocket(ConnectionAbortedError("aborted"))
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: fake)
    receiver = aoa.AoAReceiver("array", lambda *_a, **_k: None, {"port": 1})
    reason = receiver.run()
    assert reason and "aborted" in reason

    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: _FakeSocket())
    monkeypatch.setattr(aoa, "_IDLE_TIMEOUT", 0.05)
    reason = aoa.AoAReceiver("array", lambda *_a, **_k: None, {"port": 1}).run()
    assert reason and "no data" in reason


# --- Receiver-supplied timestamps --------------------------------------------
# Receiver-supplied timestamps may be skewed.


class _LineSocket:
    def __init__(self, payload: bytes):
        self.payload = payload

    def settimeout(self, _value):
        pass

    def recv(self, _size):
        data, self.payload = self.payload, b""
        return data

    def close(self):
        pass


def _aoa_emitted(monkeypatch, docs: list[dict]) -> list[tuple]:
    payload = b"".join(json.dumps(doc).encode() + b"\n" for doc in docs)
    monkeypatch.setattr(
        socket, "create_connection", lambda *_a, **_k: _LineSocket(payload)
    )
    emitted: list[tuple] = []
    receiver = aoa.AoAReceiver(
        "array", lambda h, ts, **kw: emitted.append((h, ts, kw)), {"port": 1}
    )
    assert receiver.run() == "socket connection closed by peer"
    return emitted


def test_aoa_frames_use_local_receive_time(monkeypatch):
    """The sidecar's own ``time`` is ignored, even a few seconds off: a
    sidecar copy stamped ahead could win the receiver merge, and the tracker
    would then drop dump1090 samples as older than it."""
    now = time.time()
    docs = [
        {"hex": IDENT, "time": now + 3.0, "clock": 111},
        {"hex": IDENT, "time": now - 3600.0, "clock": 222},
        {"hex": IDENT, "clock": 333},
    ]
    emitted = _aoa_emitted(monkeypatch, docs)
    after = time.time()
    for _h, stamp, _kw in emitted:
        assert now <= stamp <= after
    # The 12 MHz clock is untouched (TDOA).
    assert [kw["clock"] for _h, _t, kw in emitted] == [111, 222, 333]


# --- Receiver timestamps share the integrity monitor's clock -----------------


def test_dump1090_beast_frames_are_stamped_with_local_wall_time():
    """The archive labels each sample with the verdict published at or before
    its timestamp; verdicts use time.time(), so samples must too."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    # dump1090's 12 MHz clock is unrelated to unix time.
    payload = encode_beast(DF17_HEX, clock=123_456_789, rssi=-10.0)

    def serve():
        conn, _ = server.accept()
        conn.sendall(payload)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    stamps: list[float] = []
    receiver = create_receiver(
        "dump1090",
        "main",
        lambda _hex, stamp, **_kw: stamps.append(stamp),
        {
            "tcp_connection_ip": "127.0.0.1",
            "tcp_connection_port": port,
            "format": "beast",
        },
    )
    before = time.time()
    receiver.run()
    after = time.time()
    server.close()
    assert len(stamps) == 1
    assert before <= stamps[0] <= after
