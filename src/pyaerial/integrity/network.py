"""Opt-in comparison of local tracks against a public ADS-B aggregator.

A local spoofer only reaches local receivers, so the wider network either has
no such aircraft or has it somewhere else. Requests leave the machine (the
ICAO address goes to the configured URL), so this is off by default. If this
station feeds the same aggregator, its own (possibly spoofed) data comes back;
point ``url`` at a network you do not feed.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

import requests

from pyaerial.config.schema import CrossNetworkConfig

log = logging.getLogger("pyaerial.integrity.network")

_MAX_QUEUE = 64


@dataclass(frozen=True, slots=True)
class NetworkSighting:
    checked_at: float
    found: bool
    latitude: float | None = None
    longitude: float | None = None
    # Wall time of the aggregator's position.
    position_time: float | None = None


def parse_aggregator(doc: object, now: float) -> NetworkSighting:
    """Read a readsb-style ``{"ac": [...]}`` response."""
    if not isinstance(doc, dict):
        return NetworkSighting(now, False)
    aircraft = doc.get("ac") or doc.get("aircraft") or []
    if not isinstance(aircraft, list):
        return NetworkSighting(now, False)
    for entry in aircraft:
        if not isinstance(entry, dict):
            continue
        lat = entry.get("lat")
        lon = entry.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        seen = entry.get("seen_pos", entry.get("seen", 0))
        age = float(seen) if isinstance(seen, (int, float)) else 0.0
        return NetworkSighting(now, True, float(lat), float(lon), now - age)
    return NetworkSighting(now, bool(aircraft))


class NetworkChecker:
    """Background, rate-limited lookups; results are read without blocking."""

    def __init__(self, config: CrossNetworkConfig):
        self.config = config
        self._lock = threading.Lock()
        self._wanted: dict[str, None] = {}
        self._results: dict[str, NetworkSighting] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._session = requests.Session()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="integrity-network", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request(self, icao: str, now: float) -> None:
        """Queue *icao* unless it was checked within ``recheck_seconds``."""
        key = icao.lower()
        with self._lock:
            last = self._results.get(key)
            if last is not None and now - last.checked_at < self.config.recheck_seconds:
                return
            if key in self._wanted or len(self._wanted) >= _MAX_QUEUE:
                return
            self._wanted[key] = None
        self._wake.set()

    def result(self, icao: str) -> NetworkSighting | None:
        with self._lock:
            return self._results.get(icao.lower())

    def forget(self, icao: str) -> None:
        with self._lock:
            self._results.pop(icao.lower(), None)
            self._wanted.pop(icao.lower(), None)

    def _next(self) -> str | None:
        with self._lock:
            if not self._wanted:
                return None
            key = next(iter(self._wanted))
            del self._wanted[key]
            return key

    def _run(self) -> None:
        while not self._stop.is_set():
            icao = self._next()
            if icao is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            sighting = self._fetch(icao)
            if sighting is not None:
                with self._lock:
                    self._results[icao] = sighting
            if self._stop.wait(self.config.min_interval_seconds):
                return

    def _fetch(self, icao: str) -> NetworkSighting | None:
        url = self.config.url.replace("{icao}", icao)
        try:
            response = self._session.get(url, timeout=self.config.timeout_seconds)
            response.raise_for_status()
            return parse_aggregator(response.json(), time.time())
        except (requests.RequestException, ValueError) as exc:
            log.debug("Cross-network lookup for %s failed: %s", icao, exc)
            return None
