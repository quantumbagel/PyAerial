"""Where each receiver's antenna is, for geometry-based integrity checks.

Positions come from, in order: ``receivers.<name>.position`` in the config, the
receiver's own ``receiver.json`` (dump1090-fa / readsb publish their configured
location there), then ``home``. Fetched positions are refreshed periodically so
a receiver that moves (vehicle, drone) keeps the checks honest. Receiver
plugins that know their own location (GPS, MAVLink) can call
:meth:`ReceiverPositions.set` directly.
"""

from __future__ import annotations

import email.utils
import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import TypeGuard

import requests
import urllib3

from pyaerial.config.schema import Config, ReceiverConfig
from pyaerial.urls import redact_url

log = logging.getLogger("pyaerial.receivers.positions")

SOURCE_CONFIG = "config"
SOURCE_RECEIVER_JSON = "receiver.json"
SOURCE_HOME = "home"
SOURCE_RUNTIME = "runtime"

_DEFAULT_JSON_PORT = 8080
_REFRESH_SECONDS = 60.0
_FETCH_TIMEOUT = 3.0
_MAX_BACKOFF_SECONDS = 900.0
_MAX_BODY_BYTES = 1 << 20
_JOIN_TIMEOUT = 2.0


class FetchError(Exception):
    """An HTTP fetch that failed; ``retry_after`` is the server's ask (s)."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value: object) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value.strip())
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    return max(0.0, seconds) if math.isfinite(seconds) else None


class Backoff:
    """Exponential backoff for one endpoint, on the monotonic clock."""

    def __init__(self, base: float, maximum: float):
        self.base = base
        self.maximum = maximum
        self.failures = 0
        self.until = 0.0

    def ready(self, now: float) -> bool:
        return now >= self.until

    def success(self, now: float, interval: float) -> None:
        self.failures = 0
        self.until = now + interval

    def failure(self, now: float, retry_after: float | None = None) -> float:
        """Record a failure; return the delay before the next attempt."""
        self.failures += 1
        delay = min(self.maximum, self.base * 2 ** min(self.failures - 1, 30))
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.maximum))
        self.until = now + delay
        return delay


def fetch_json(
    url: str,
    *,
    timeout: float,
    session: requests.Session | None = None,
    max_bytes: int = _MAX_BODY_BYTES,
) -> object:
    """GET *url* and parse its JSON body, raising :class:`FetchError`.

    *timeout* bounds connecting and each read, and the whole body: a server
    trickling bytes cannot hold the caller past it (``requests`` alone only
    times out single reads). Redirects are not followed (one could move an
    https request to plain http). Error messages name only ``scheme://host``:
    the URL's path or query may carry an API key.
    """
    deadline = time.monotonic() + timeout
    where = redact_url(url)
    get = session.get if session is not None else requests.get
    try:
        response = get(url, timeout=timeout, stream=True, allow_redirects=False)
    except requests.RequestException as exc:
        raise FetchError(f"{type(exc).__name__} from {where}") from exc
    with response:
        status = response.status_code
        if 300 <= status < 400:
            raise FetchError(f"HTTP {status} redirect from {where} (redirects are not followed)")
        if status >= 400:
            retry_after = None
            if status in (429, 503):
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
            raise FetchError(f"HTTP {status} from {where}", retry_after=retry_after)
        body = bytearray()
        try:
            # read1 returns what has arrived, so the deadline is checked
            # between every network read.
            while chunk := response.raw.read1(65536, decode_content=True):
                body += chunk
                if len(body) > max_bytes:
                    raise FetchError(f"response from {where} larger than {max_bytes} bytes")
                if time.monotonic() > deadline:
                    raise FetchError(f"no complete response from {where} within {timeout:g} s")
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
            raise FetchError(f"{type(exc).__name__} reading from {where}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise FetchError(f"invalid JSON from {where}") from exc


@dataclass(frozen=True, slots=True)
class ReceiverPosition:
    latitude: float
    longitude: float
    # Metres above mean sea level.
    altitude: float = 0.0
    # Metres above ground, for the radio horizon.
    antenna_height: float = 10.0
    source: str = SOURCE_HOME
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "antenna_height": self.antenna_height,
            "source": self.source,
            "updated_at": self.updated_at,
        }


def default_position_url(cfg: ReceiverConfig) -> str | None:
    """``receiver.json`` URL for a dump1090-style receiver, if one is implied."""
    if cfg.position_url:
        return cfg.position_url
    if cfg.type != "dump1090":
        return None
    host = cfg.host or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{_DEFAULT_JSON_PORT}/data/receiver.json"


def parse_receiver_json(doc: object) -> tuple[float, float, float | None] | None:
    """``(lat, lon, alt_m)`` from a dump1090-fa / readsb ``receiver.json``."""
    if not isinstance(doc, dict):
        return None
    lat = doc.get("lat")
    lon = doc.get("lon")
    if not _is_number(lat) or not _is_number(lon):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    # dump1090 writes 0,0 when no location is configured.
    if lat == 0 and lon == 0:
        return None
    alt = doc.get("alt_m_amsl", doc.get("alt"))
    altitude = float(alt) if _is_number(alt) and math.isfinite(alt) else None
    return float(lat), float(lon), altitude


def _is_number(value: object) -> TypeGuard[int | float]:
    # JSON true/false are ints to isinstance.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class ReceiverPositions:
    """Thread-safe map of receiver name to antenna position."""

    def __init__(self, config: Config, *, fetch: bool = True):
        self._config = config
        self._lock = threading.Lock()
        self._positions: dict[str, ReceiverPosition] = {}
        self._urls: dict[str, str] = {}
        self._backoff: dict[str, Backoff] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        home = config.home
        for name, cfg in config.receivers.items():
            if cfg.position is not None:
                self._positions[name] = ReceiverPosition(
                    cfg.position.latitude,
                    cfg.position.longitude,
                    cfg.position.altitude,
                    cfg.antenna_height,
                    SOURCE_CONFIG,
                    time.time(),
                )
                continue
            self._positions[name] = ReceiverPosition(
                home.latitude,
                home.longitude,
                0.0,
                cfg.antenna_height,
                SOURCE_HOME,
                time.time(),
            )
            url = default_position_url(cfg) if fetch else None
            if url:
                self._urls[name] = url
                self._backoff[name] = Backoff(_REFRESH_SECONDS, _MAX_BACKOFF_SECONDS)

    def get(self, name: str | None) -> ReceiverPosition:
        with self._lock:
            if name and name in self._positions:
                return self._positions[name]
        home = self._config.home
        return ReceiverPosition(home.latitude, home.longitude)

    def all(self) -> dict[str, ReceiverPosition]:
        with self._lock:
            return dict(self._positions)

    def set(
        self,
        name: str,
        latitude: float,
        longitude: float,
        altitude: float | None = None,
        *,
        source: str = SOURCE_RUNTIME,
    ) -> None:
        """Record a new antenna position (for moving receivers)."""
        with self._lock:
            previous = self._positions.get(name)
            cfg = self._config.receivers.get(name)
            height = cfg.antenna_height if cfg is not None else 10.0
            if altitude is None:
                altitude = previous.altitude if previous is not None else 0.0
            self._positions[name] = ReceiverPosition(
                latitude, longitude, altitude, height, source, time.time()
            )

    def start(self) -> None:
        """Begin polling ``receiver.json`` for receivers without a fixed position."""
        if not self._urls or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._poll, name="receiver-positions", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = _JOIN_TIMEOUT) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.debug("Receiver position poller still busy after %.1f s", timeout)

    def refresh(self, now: float | None = None) -> None:
        """Fetch every ``receiver.json`` that is due (not backing off)."""
        now = time.monotonic() if now is None else now
        for name, url in list(self._urls.items()):
            backoff = self._backoff[name]
            if not backoff.ready(now):
                continue
            try:
                fetched = self._fetch(url)
            except FetchError as exc:
                delay = backoff.failure(now, exc.retry_after)
                log.debug("No receiver position: %s (retry in %.0f s)", exc, delay)
                continue
            backoff.success(now, _REFRESH_SECONDS)
            if fetched is None:
                continue
            lat, lon, alt = fetched
            previous = self.get(name)
            if previous.source == SOURCE_HOME:
                log.info(
                    "Receiver %s position %.5f, %.5f from %s",
                    name,
                    lat,
                    lon,
                    redact_url(url),
                )
            self.set(name, lat, lon, alt, source=SOURCE_RECEIVER_JSON)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            due = min(b.until for b in self._backoff.values()) - time.monotonic()
            if self._stop.wait(max(1.0, due)):
                return

    @staticmethod
    def _fetch(url: str) -> tuple[float, float, float | None] | None:
        """Position from *url*; ``None`` if it has none; FetchError on failure."""
        return parse_receiver_json(fetch_json(url, timeout=_FETCH_TIMEOUT))
