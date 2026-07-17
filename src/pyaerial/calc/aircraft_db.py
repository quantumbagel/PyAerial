"""
Aircraft metadata lookup backed by SQLite.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("pyaerial.aircraft_db")


import requests


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
        self.path = Path(path)
        # Ensure parent directory exists
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        
        # Open in read-write mode to allow dynamic caching, and allow shared thread access
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS aircraft (icao TEXT PRIMARY KEY, data TEXT)"
        )
        self._conn.commit()

    @property
    def available(self) -> bool:
        return self._conn is not None

    def lookup(self, icao: str) -> dict | None:
        """Return aircraft metadata for an ICAO hex, checking local index first, then API."""
        if self._conn is None:
            return None

        icao = icao.lower().strip()
        if not icao:
            return None

        # 1. Check local SQLite database/cache
        try:
            row = self._conn.execute(
                "SELECT data FROM aircraft WHERE icao = ?", (icao,)
            ).fetchone()
        except sqlite3.Error as e:
            log.warning("Database error during lookup for %s: %s", icao, e)
            row = None

        if row:
            if row[0] is None:
                # Cached negative lookup
                return None
            try:
                record = json.loads(row[0])
                if record is not None:
                    # If we already checked Planespotters.net, return the cached result
                    if "photo_checked" in record:
                        return self._return_normalized_record(icao, record)

                    # Otherwise, attempt to enrich with a photo and save
                    photo_info = self._fetch_photo_from_planespotters(icao, record.get("registration"))
                    record.update(photo_info)
                    record["photo_checked"] = True
                    self._update_cache(icao, record)
                    return normalize_record_photos(record)
            except Exception as e:
                log.warning("Error parsing cached record for %s: %s", icao, e)

        # 2. Cache miss: Fetch from HexDB and Planespotters APIs
        log.info("Aircraft DB miss for %s. Querying online APIs...", icao)
        record = self._fetch_from_apis(icao)

        # Cache results (even if None/empty, to prevent spamming APIs for invalid ICAOs)
        self._update_cache(icao, record)
        return normalize_record_photos(record) if record is not None else None

    def _return_normalized_record(self, icao: str, record: dict) -> dict:
        raw_url = record.get("photo_url")
        normalized = normalize_record_photos(record)
        if normalized.get("photo_url") != raw_url:
            self._update_cache(icao, normalized)
        return normalized

    def _fetch_from_apis(self, icao: str) -> dict | None:
        headers = {"User-Agent": "PyAerial/2.0 (https://github.com/quantumbagel/PyAerial)"}
        
        # 1. Fetch from HexDB
        hexdb_data = {}
        try:
            resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao}", headers=headers, timeout=3.0)
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
        except Exception as e:
            log.warning("HexDB API lookup failed for %s: %s", icao, e)
            return None

        # 2. Fetch from Planespotters
        registration = hexdb_data.get("registration")
        photo_info = self._fetch_photo_from_planespotters(icao, registration)
        hexdb_data.update(photo_info)
        hexdb_data["photo_checked"] = True

        return hexdb_data

    def _fetch_photo_from_planespotters(self, icao: str, registration: str | None = None) -> dict:
        headers = {"User-Agent": "PyAerial/2.0 (https://github.com/quantumbagel/PyAerial)"}
        photo_info = {
            "photo_url": None,
            "photo_link": None,
            "photo_photographer": None
        }
        
        # Try looking up by ICAO hex first
        try:
            resp = requests.get(f"https://api.planespotters.net/pub/photos/hex/{icao}", headers=headers, timeout=3.0)
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
                resp = requests.get(f"https://api.planespotters.net/pub/photos/reg/{registration}", headers=headers, timeout=3.0)
                if resp.status_code == 200:
                    data = resp.json()
                    photos = data.get("photos", [])
                    if photos:
                        photo = photos[0]
                        photo_info["photo_url"] = _photo_thumbnail_url(photo)
                        photo_info["photo_link"] = photo.get("link")
                        photo_info["photo_photographer"] = photo.get("photographer")
            except Exception as e:
                log.debug("Planespotters.net registration lookup failed for %s: %s", registration, e)

        return photo_info

    def _update_cache(self, icao: str, record: dict | None) -> None:
        try:
            val = json.dumps(record) if record is not None else None
            self._conn.execute(
                "INSERT OR REPLACE INTO aircraft (icao, data) VALUES (?, ?)",
                (icao, val)
            )
            self._conn.commit()
        except sqlite3.Error as e:
            log.warning("Failed to update SQLite cache for %s: %s", icao, e)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


