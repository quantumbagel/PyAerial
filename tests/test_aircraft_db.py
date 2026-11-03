"""The local aircraft database cache: corrupt files, closed handles, locks."""

from __future__ import annotations

import logging
import sqlite3
import time

from pyaerial.enrich.aircraft_db import AircraftDB

# --- Aircraft cache ----------------------------------------------------------


def test_corrupt_aircraft_cache_does_not_stop_the_engine(tmp_path, caplog):
    path = tmp_path / "aircraft.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with caplog.at_level(logging.WARNING, logger="pyaerial.aircraft_db"):
        db = AircraftDB(path)
    assert db.available is False
    assert db.lookup_cached("abc123") is None
    assert db.is_cached("abc123") is False
    db.close()
    assert "aircraft.db" in caplog.text


def test_aircraft_cache_read_after_close_is_safe(tmp_path):
    db = AircraftDB(tmp_path / "aircraft.db")
    db.close()
    # A lookup thread that passed the availability check before close().
    with db._lock:
        assert db._read_cached_record("abc123") is not None


# --- Opening without a write lock --------------------------------------------


def test_aircraft_db_opens_without_a_write_lock(tmp_path):
    path = tmp_path / "aircraft.db"
    AircraftDB(path).close()
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO aircraft VALUES ('abc123', NULL)")
    try:
        started = time.monotonic()
        db = AircraftDB(path)
        assert db.available is True
        assert time.monotonic() - started < 1.0
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    db.close()
