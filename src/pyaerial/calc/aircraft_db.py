"""
Aircraft metadata lookup backed by SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import requests

log = logging.getLogger("pyaerial.aircraft_db")

_MISSING = object()


def normalize_photo_url(value: object) -> str | None:
    """Return a thumbnail URL string from Planespotters API values (string or {src})."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, dict):
        src = value.get("src")
        if isinstance(src, str):
            src = src.strip()
            return src or None
    return None


def _photo_thumbnail_url(photo: dict) -> str | None:
    for key in ("thumbnail_large", "thumbnail", "thumbnail_small"):
        url = normalize_photo_url(photo.get(key))
        if url:
            return url
    return None


def normalize_record_photos(record: dict) -> dict:
    """Ensure photo_url is a URL string, not a Planespotters thumbnail object."""
    raw_url = record.get("photo_url")
    normalized = normalize_photo_url(raw_url)
    if normalized != raw_url:
        record["photo_url"] = normalized
    return record


class AircraftDB:
    """Lookup of aircraft metadata by ICAO hex, backed by SQLite (local or dynamic API cache)."""

    def __init__(self, path: str | Path):
        path = Path(path)
        if not path.is_absolute():
            # Resolve relative to the project root directory
            project_root = Path(__file__).resolve().parent.parent.parent.parent
            path = (project_root / path).resolve()

        self.path = path
        # Ensure parent directory exists
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        # Open in read-write mode to allow dynamic caching, and allow shared thread access
        with self._lock:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS aircraft (icao TEXT PRIMARY KEY, data TEXT)"
            )
            self._conn.commit()

    @property
    def available(self) -> bool:
        return self._conn is not None

    def is_cached(self, icao: str) -> bool:
        """Return True if aircraft metadata for icao is cached in SQLite (including photo check)."""
        if self._conn is None:
            return False
        icao = icao.lower().strip()
        if not icao:
            return True
        with self._lock:
            record = self._read_cached_record(icao)
        if record is _MISSING:
            return False
        if record is None:
            return True
        return "photo_checked" in record

    def lookup_cached_fast(self, icao: str) -> dict | None:
        """Return cached aircraft metadata from SQLite without making network requests."""
        if self._conn is None:
            return None
        icao = icao.lower().strip()
        if not icao:
            return None
        with self._lock:
            record = self._read_cached_record(icao)
        if record is _MISSING or record is None:
            return None
        if isinstance(record, dict):
            return normalize_record_photos(record)
        return None

    def lookup_cached(self, icao: str) -> dict | None:
        """Return aircraft metadata for an ICAO hex, checking local index first, then API."""
        if self._conn is None:
            return None

        icao = icao.lower().strip()
        if not icao:
            return None

        with self._lock:
            record = self._read_cached_record(icao)
        if record is not _MISSING:
            if record is None:
                return None
            if "photo_checked" in record:
                return self._return_normalized_record(icao, record)

            photo_info = self._fetch_photo_from_planespotters(
                icao, record.get("registration")
            )
            record.update(photo_info)
            record["photo_checked"] = True
            self._update_cache(icao, record)
            return normalize_record_photos(record)

        # Cache miss: Fetch from HexDB and Planespotters APIs
        log.info("Aircraft DB miss for %s. Querying online APIs...", icao)
        try:
            record = self._fetch_from_apis(icao)
        except Exception as exc:
            # Transport / 5xx — do not persist a negative cache entry.
            log.warning("Aircraft API lookup failed for %s: %s", icao, exc)
            return None

        # Cache results (including 404/empty) so we do not spam APIs for invalid ICAOs.
        self._update_cache(icao, record)
        return normalize_record_photos(record) if record is not None else None

    def _read_cached_record(self, icao: str) -> dict | None | object:
        """Return cached record, None for a negative cache entry, or _MISSING."""
        try:
            row = self._conn.execute(
                "SELECT data FROM aircraft WHERE icao = ?", (icao,)
            ).fetchone()
        except sqlite3.Error as e:
            log.warning("Database error during lookup for %s: %s", icao, e)
            return _MISSING

        if not row:
            return _MISSING
        if row[0] is None:
            return None

        try:
            record = json.loads(row[0])
        except Exception as e:
            log.warning("Error parsing cached record for %s: %s", icao, e)
            return _MISSING
        return record if isinstance(record, dict) or record is None else _MISSING

    def _return_normalized_record(self, icao: str, record: dict) -> dict:
        raw_url = record.get("photo_url")
        normalized = normalize_record_photos(record)
        if normalized.get("photo_url") != raw_url:
            self._update_cache(icao, normalized)
        return normalized

    def _fetch_from_apis(self, icao: str) -> dict | None:
        headers = {
            "User-Agent": "PyAerial/2.0 (https://github.com/quantumbagel/PyAerial)"
        }

        # 1. Fetch from HexDB
        hexdb_data = {}
        try:
            resp = requests.get(
                f"https://hexdb.io/api/v1/aircraft/{icao}", headers=headers, timeout=3.0
            )
            if resp.status_code == 200:
                data = resp.json()
                hexdb_data = {
                    "icao": icao,
                    "registration": data.get("Registration"),
                    "manufacturer_name": data.get("Manufacturer"),
                    "model": data.get("Type"),
                    "owner": data.get("RegisteredOwners"),
                    "country": data.get("Registered"),
                    "typecode": data.get("ICAOTypeCode"),
                    "operator_callsign": data.get("OperatorFlagCode"),
                    "callsign": data.get("Registration"),
                }
            elif resp.status_code == 404:
                log.debug("ICAO %s not found in HexDB", icao)
                return None
            resp.raise_for_status()
        except Exception as e:
            log.warning("HexDB API lookup failed for %s: %s", icao, e)
            raise

        # 2. Fetch from Planespotters
        registration = hexdb_data.get("registration")
        photo_info = self._fetch_photo_from_planespotters(icao, registration)
        hexdb_data.update(photo_info)
        hexdb_data["photo_checked"] = True

        return hexdb_data

    def _fetch_photo_from_planespotters(
        self, icao: str, registration: str | None = None
    ) -> dict:
        headers = {
            "User-Agent": "PyAerial/2.0 (https://github.com/quantumbagel/PyAerial)"
        }
        photo_info = {"photo_url": None, "photo_link": None, "photo_photographer": None}

        # Try looking up by ICAO hex first
        try:
            resp = requests.get(
                f"https://api.planespotters.net/pub/photos/hex/{icao}",
                headers=headers,
                timeout=3.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                photos = data.get("photos", [])
                if photos:
                    photo = photos[0]
                    photo_info["photo_url"] = _photo_thumbnail_url(photo)
                    photo_info["photo_link"] = photo.get("link")
                    photo_info["photo_photographer"] = photo.get("photographer")
                    return photo_info
        except Exception as e:
            log.debug("Planespotters.net hex lookup failed for %s: %s", icao, e)

        # Try looking up by Registration if we have it and hex lookup failed/had no photos
        if registration:
            try:
                resp = requests.get(
                    f"https://api.planespotters.net/pub/photos/reg/{registration}",
                    headers=headers,
                    timeout=3.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    photos = data.get("photos", [])
                    if photos:
                        photo = photos[0]
                        photo_info["photo_url"] = _photo_thumbnail_url(photo)
                        photo_info["photo_link"] = photo.get("link")
                        photo_info["photo_photographer"] = photo.get("photographer")
            except Exception as e:
                log.debug(
                    "Planespotters.net registration lookup failed for %s: %s",
                    registration,
                    e,
                )

        return photo_info

    def _update_cache(self, icao: str, record: dict | None) -> None:
        try:
            val = json.dumps(record)
            with self._lock:
                if self._conn is not None:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO aircraft (icao, data) VALUES (?, ?)",
                        (icao, val),
                    )
                    self._conn.commit()
        except sqlite3.Error as e:
            log.warning("Failed to update SQLite cache for %s: %s", icao, e)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
