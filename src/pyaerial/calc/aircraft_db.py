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
        with csv_path.open(newline="") as handle:
            reader = csv.reader(handle, quotechar="'")
            batch: list[tuple[str, str]] = []
            for row in reader:
                if not row:
                    continue
                record = {header[i]: value for i, value in enumerate(row) if i < len(header)}
                icao = record.get("icao", "").strip().lower()
                if not icao:
                    continue
                batch.append((icao, json.dumps(record)))
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
