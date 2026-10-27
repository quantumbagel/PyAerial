"""Where each receiver's antenna is, for geometry-based integrity checks.

Positions come from, in order: ``receivers.<name>.position`` in the config, the
receiver's own ``receiver.json`` (dump1090-fa / readsb publish their configured
location there), then ``home``. Fetched positions are refreshed periodically so
a receiver that moves (vehicle, drone) keeps the checks honest. Receiver
plugins that know their own location (GPS, MAVLink) can call
:meth:`ReceiverPositions.set` directly.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

import requests

from pyaerial.config.schema import Config, ReceiverConfig

log = logging.getLogger("pyaerial.receivers.positions")

SOURCE_CONFIG = "config"
SOURCE_RECEIVER_JSON = "receiver.json"
SOURCE_HOME = "home"
SOURCE_RUNTIME = "runtime"

_DEFAULT_JSON_PORT = 8080
_REFRESH_SECONDS = 60.0
_FETCH_TIMEOUT = 3.0


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
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    # dump1090 writes 0,0 when no location is configured.
    if lat == 0 and lon == 0:
        return None
    alt = doc.get("alt_m_amsl", doc.get("alt"))
    altitude = float(alt) if isinstance(alt, (int, float)) and math.isfinite(alt) else None
    return float(lat), float(lon), altitude


class ReceiverPositions:
    """Thread-safe map of receiver name to antenna position."""

    def __init__(self, config: Config, *, fetch: bool = True):
        self._config = config
        self._lock = threading.Lock()
        self._positions: dict[str, ReceiverPosition] = {}
        self._urls: dict[str, str] = {}
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

    def stop(self) -> None:
        self._stop.set()

    def refresh(self) -> None:
        for name, url in list(self._urls.items()):
            fetched = self._fetch(url)
            if fetched is None:
                continue
            lat, lon, alt = fetched
            previous = self.get(name)
            if previous.source == SOURCE_HOME:
                log.info(
                    "Receiver %s position %.5f, %.5f from %s", name, lat, lon, url
                )
            self.set(name, lat, lon, alt, source=SOURCE_RECEIVER_JSON)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            if self._stop.wait(_REFRESH_SECONDS):
                return

    @staticmethod
    def _fetch(url: str) -> tuple[float, float, float | None] | None:
        try:
            response = requests.get(url, timeout=_FETCH_TIMEOUT)
            response.raise_for_status()
            return parse_receiver_json(response.json())
        except (requests.RequestException, ValueError) as exc:
            log.debug("No receiver position from %s: %s", url, exc)
            return None
