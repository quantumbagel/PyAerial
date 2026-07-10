"""
Aircraft metadata lookup backed by SQLite.

The OpenSky ``database.csv`` export is ~90MB; the old code linearly scanned it on
every lookup. Instead we build a one-time SQLite index keyed by ICAO
(:func:`build_from_csv`) and perform O(1) lookups (:class:`AircraftDB`).
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path

from pyaerial.constants import STORE_OPENSKY_HEADER

log = logging.getLogger("pyaerial.aircraft_db")


class AircraftDB:
    """Read-only lookup of aircraft metadata by ICAO hex."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        if self.path.exists():
            # Read-only, shared across threads (queries are independent).
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                         check_same_thread=False)
        else:
            log.warning("Aircraft database %s not found; ICAO metadata lookups disabled. "
                        "Run 'pyaerial build-db' to create it.", self.path)

    @property
    def available(self) -> bool:
        return self._conn is not None

    def lookup(self, icao: str) -> dict | None:
        """Return aircraft metadata for an ICAO hex, or ``None`` if unknown."""
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT data FROM aircraft WHERE icao = ?", (icao.lower(),)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def build_from_csv(csv_path: str | Path, db_path: str | Path, *,
                   header: list[str] = STORE_OPENSKY_HEADER) -> int:
    """
    Build (or rebuild) the SQLite aircraft index from an OpenSky CSV export.

    :return: number of aircraft rows indexed
    """
    csv_path = Path(csv_path)
    db_path = Path(db_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"aircraft CSV {csv_path} does not exist")

    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE aircraft (icao TEXT PRIMARY KEY, data TEXT NOT NULL)")
        count = 0
        with csv_path.open(newline="", encoding="utf-8", errors="ignore") as handle:
            reader = csv.reader(handle)
            first_row = next(reader, None)
            if not first_row:
                return 0

            # Determine the CSV header
            first_col = first_row[0].strip().lower().lstrip('\ufeff').replace('"', '').replace("'", "")
            if first_col in ("icao24", "icao"):
                csv_header = [col.strip().lower().replace('"', '').replace("'", "") for col in first_row]
                has_header = True
            else:
                csv_header = [col.lower() for col in header]
                has_header = False

            batch: list[tuple[str, str]] = []

            def get_icao_and_record(row_data):
                record = {}
                for i, val in enumerate(row_data):
                    if i < len(csv_header):
                        col = csv_header[i]
                        record[col] = val.strip()

                # Set unified key for icao
                icao = record.get("icao") or record.get("icao24")
                if icao:
                    icao = icao.lower()
                    record["icao"] = icao
                else:
                    return None

                # Map database.csv fields to standard keys expected by the application:
                # callsign, country, built, manufacturer_name, model, owner, registration
                if "manufacturername" in record:
                    record["manufacturer_name"] = record["manufacturername"]
                
                if "operatorcallsign" in record:
                    record["operator_callsign"] = record["operatorcallsign"]
                    if "callsign" not in record or not record["callsign"]:
                        record["callsign"] = record["operatorcallsign"]

                return icao, json.dumps(record)

            if not has_header:
                res = get_icao_and_record(first_row)
                if res:
                    batch.append(res)

            for row in reader:
                if not row:
                    continue
                res = get_icao_and_record(row)
                if res:
                    batch.append(res)
                if len(batch) >= 10_000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO aircraft VALUES (?, ?)", batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                conn.executemany("INSERT OR REPLACE INTO aircraft VALUES (?, ?)", batch)
                count += len(batch)
        conn.commit()
    finally:
        conn.close()

    log.info("Indexed %d aircraft into %s", count, db_path)
    return count

