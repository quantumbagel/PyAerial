"""Network lookups used by integrity: receiver.json polling and the
cross-network check (deadlines, backoff, Retry-After, redirects, redaction)."""

from __future__ import annotations

import email.utils
import logging
import socket
import threading
import time

import pytest

from helpers import make_config
from pyaerial.config.schema import CrossNetworkConfig, ReceiverConfig
from pyaerial.integrity.network import NetworkChecker, NetworkSighting
from pyaerial.receivers.positions import (
    SOURCE_RECEIVER_JSON,
    Backoff,
    FetchError,
    ReceiverPositions,
    fetch_json,
    retry_after_seconds,
)

# --- Cross-network results for forgotten aircraft ----------------------------


def test_network_result_dropped_when_forgotten_mid_fetch():
    started, release = threading.Event(), threading.Event()
    checker = NetworkChecker(
        CrossNetworkConfig(enabled=True, min_interval_seconds=0.01)
    )

    def fetch(icao: str) -> NetworkSighting:
        started.set()
        release.wait(5)
        return NetworkSighting(time.time(), False)

    checker._fetch = fetch
    checker.start()
    try:
        checker.request("ABC123", time.time())
        assert started.wait(5)
        checker.forget("ABC123")
        release.set()
        deadline = time.time() + 5
        while checker._inflight is not None and time.time() < deadline:
            time.sleep(0.01)
        assert checker.result("ABC123") is None
    finally:
        checker.stop()


# --- Deadlines, backoff (Retry-After) and bounded stop -----------------------


def _serve(handler) -> str:
    """One-shot HTTP server on localhost running *handler(conn)*; returns its URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def run() -> None:
        conn, _ = sock.accept()
        try:
            conn.recv(65536)
            handler(conn)
        except OSError:
            pass
        finally:
            conn.close()
            sock.close()

    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{sock.getsockname()[1]}/data/receiver.json"


def _respond(status: str, body: bytes = b"", headers: str = ""):
    def handler(conn: socket.socket) -> None:
        conn.sendall(
            f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n{headers}\r\n".encode()
            + body
        )

    return handler


# --- fetch_json ---------------------------------------------------------------


def test_fetch_json_parses_body():
    url = _serve(_respond("200 OK", b'{"lat": 35.1, "lon": -78.2}'))
    assert fetch_json(url, timeout=2.0) == {"lat": 35.1, "lon": -78.2}


def test_fetch_json_has_an_overall_deadline():
    """A server trickling one byte every 0.1 s never trips a per-read timeout."""

    def trickle(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(1000):
            conn.sendall(b" ")
            time.sleep(0.1)

    url = _serve(trickle)
    start = time.monotonic()
    with pytest.raises(FetchError, match="within"):
        fetch_json(url, timeout=0.5)
    assert time.monotonic() - start < 1.5


@pytest.mark.parametrize(
    ("status", "header", "expected"),
    [
        ("429 Too Many Requests", "Retry-After: 7\r\n", 7.0),
        ("503 Service Unavailable", "Retry-After: 12\r\n", 12.0),
        ("429 Too Many Requests", "", None),
        ("404 Not Found", "Retry-After: 7\r\n", None),
    ],
)
def test_fetch_json_reports_retry_after(status, header, expected):
    url = _serve(_respond(status, headers=header))
    with pytest.raises(FetchError) as caught:
        fetch_json(url, timeout=2.0)
    assert caught.value.retry_after == expected


def test_fetch_json_rejects_oversized_and_invalid_bodies():
    url = _serve(_respond("200 OK", b"[" + b"1," * 1000 + b"1]"))
    with pytest.raises(FetchError, match="larger"):
        fetch_json(url, timeout=2.0, max_bytes=100)
    url = _serve(_respond("200 OK", b"<html>"))
    with pytest.raises(FetchError, match="JSON"):
        fetch_json(url, timeout=2.0)


def test_retry_after_parsing():
    assert retry_after_seconds("30") == 30.0
    assert retry_after_seconds("-5") == 0.0
    later = email.utils.formatdate(time.time() + 60, usegmt=True)
    assert retry_after_seconds(later) == pytest.approx(60, abs=2)
    assert retry_after_seconds("soon") is None
    assert retry_after_seconds(None) is None


def test_backoff_doubles_honours_retry_after_and_resets():
    backoff = Backoff(2.0, 60.0)
    assert [backoff.failure(0.0) for _ in range(6)] == [2, 4, 8, 16, 32, 60]
    assert not backoff.ready(59.0) and backoff.ready(60.0)
    assert backoff.failure(0.0, retry_after=5.0) == 60.0  # never shorter
    backoff.success(100.0, 2.0)
    assert backoff.failures == 0 and backoff.until == 102.0
    assert backoff.failure(0.0, retry_after=30.0) == 30.0
    assert backoff.failure(0.0, retry_after=1e9) == 60.0  # capped


# --- NetworkChecker -----------------------------------------------------------


def _checker(fetch) -> NetworkChecker:
    checker = NetworkChecker(
        CrossNetworkConfig(enabled=True, min_interval_seconds=0.05)
    )
    checker._fetch = fetch
    return checker


def test_network_checker_backs_off_on_failures():
    calls: list[float] = []

    def failing(icao: str):
        calls.append(time.monotonic())
        raise FetchError("down")

    checker = _checker(failing)
    for i in range(40):
        checker.request(f"{i:06x}", time.time())
    checker.start()
    time.sleep(1.2)
    checker.stop()
    # 0.05 s apart without backoff would be ~24 calls; doubling gives ~5.
    assert 3 <= len(calls) <= 6
    gaps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert all(
        later > earlier * 1.5 for earlier, later in zip(gaps, gaps[1:], strict=False)
    )


def test_network_checker_honours_retry_after():
    calls: list[float] = []

    def limited(icao: str):
        calls.append(time.monotonic())
        raise FetchError("HTTP 429", retry_after=30.0)

    checker = _checker(limited)
    for i in range(5):
        checker.request(f"{i:06x}", time.time())
    checker.start()
    time.sleep(0.5)
    checker.stop()
    assert len(calls) == 1


def test_network_checker_stop_joins_within_bound():
    release = threading.Event()

    def hung(icao: str):
        release.wait(10)
        raise FetchError("late")

    checker = _checker(hung)
    checker.start()
    checker.stop()
    assert not checker._thread.is_alive()

    checker = _checker(hung)
    checker.request("abc123", time.time())
    checker.start()
    time.sleep(0.1)
    start = time.monotonic()
    checker.stop(timeout=0.3)
    assert time.monotonic() - start < 1.0
    release.set()
    checker._thread.join(2)
    assert not checker._thread.is_alive()


# --- receiver.json polling ------------------------------------------------------


def _positions() -> ReceiverPositions:
    config = make_config(
        receivers={"auto": ReceiverConfig(type="dump1090", host="feeder")}
    )
    return ReceiverPositions(config)


def test_receiver_json_polling_backs_off(monkeypatch):
    calls: list[float] = []
    answers: list[object] = []

    def fake_fetch(url: str):
        calls.append(url)
        answer = answers.pop(0) if answers else (35.9, -78.9, None)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ReceiverPositions, "_fetch", staticmethod(fake_fetch))
    positions = _positions()
    answers[:] = [
        FetchError("refused"),
        FetchError("refused"),
        FetchError("429", retry_after=600.0),
    ]
    t = 0.0
    attempts = []
    while t <= 2000:
        before = len(calls)
        positions.refresh(t)
        if len(calls) > before:
            attempts.append(t)
        t += 10
    # 60 s, then 120 s, then the server's 600 s, then the normal cadence.
    assert attempts[:6] == [0, 60, 180, 780, 840, 900]
    assert positions.get("auto").source == SOURCE_RECEIVER_JSON


def test_receiver_positions_stop_joins_thread(monkeypatch):
    monkeypatch.setattr(
        ReceiverPositions, "_fetch", staticmethod(lambda url: (35.9, -78.9, None))
    )
    positions = _positions()
    positions.start()
    time.sleep(0.1)
    start = time.monotonic()
    positions.stop()
    assert time.monotonic() - start < 1.0
    assert not positions._thread.is_alive()
    assert positions.get("auto").source == SOURCE_RECEIVER_JSON


# --- secrets in logs, redirects ---------------------------------------------------


def _serve_response(response: bytes) -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)

    def run() -> None:
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(response)
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_redirects_are_not_followed():
    target = _serve_response(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
    base = _serve_response(
        f"HTTP/1.1 302 Found\r\nLocation: {target}/x\r\nContent-Length: 0\r\n\r\n".encode()
    )
    with pytest.raises(FetchError, match="redirect"):
        fetch_json(f"{base}/data/receiver.json", timeout=2.0)


def test_cross_network_failures_never_log_the_url_query(caplog):
    secret = "SECRET123"
    config = CrossNetworkConfig(
        enabled=True,
        url=f"http://127.0.0.1:9/v2/hex/{{icao}}?api_key={secret}",
        min_interval_seconds=0.01,
        timeout_seconds=0.5,
    )
    checker = NetworkChecker(config)
    checker._backoff.maximum = 0.01  # retry at once
    caplog.set_level(logging.DEBUG, logger="pyaerial.integrity.network")
    checker.start()
    try:
        deadline = time.monotonic() + 10
        while checker._backoff.failures < 4 and time.monotonic() < deadline:
            checker.request(f"{checker._backoff.failures:06x}", time.time())
            time.sleep(0.02)
    finally:
        checker.stop()
    assert checker._backoff.failures >= 3
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in text and "api_key" not in text
    assert "http://127.0.0.1" in text
