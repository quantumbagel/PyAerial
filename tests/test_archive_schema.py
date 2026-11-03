"""Archive schema migrations (``store/archive/schema.py``): older, unversioned
and partially upgraded archives are upgraded in place, or refused untouched."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from helpers import (
    fine_track_plane,
    gnss_then_baro_plane,
    one_fix_plane,
    pad_plane,
    track_plane,
    with_unknown_then_ok,
)
from pyaerial.receivers.frames import RawFrame
from pyaerial.store.archive import ArchiveReader, ArchiveWriter, schema

# --- Schema migration -------------------------------------------------------

_V1_SCHEMA = """
CREATE TABLE flights (
    flight_id TEXT PRIMARY KEY,
    icao TEXT NOT NULL,
    status TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    retained INTEGER NOT NULL DEFAULT 1,
    callsign TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    aircraft_type TEXT NOT NULL DEFAULT '',
    info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE telemetry (
    flight_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    icao TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    altitude REAL,
    speed REAL,
    heading REAL,
    PRIMARY KEY (flight_id, timestamp)
);
CREATE TABLE alerts (
    alert_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL,
    icao TEXT,
    callsign TEXT,
    zone TEXT,
    rule TEXT,
    active INTEGER,
    activated_at REAL,
    deactivated_at REAL,
    eta REAL,
    reason TEXT,
    last_updated REAL,
    latitude REAL,
    longitude REAL,
    altitude REAL
);
INSERT INTO flights (flight_id, icao, status, start_time, end_time, callsign)
    VALUES ('old111-5', 'old111', 'completed', 5.0, 50.0, 'OLD1');
INSERT INTO telemetry (flight_id, timestamp, icao, latitude, longitude)
    VALUES ('old111-5', 5.0, 'old111', 35.0, -78.0);
INSERT INTO alerts (alert_id, flight_id, icao, zone, rule, activated_at)
    VALUES ('old111-5:pad:warn', 'old111-5', 'old111', 'pad', 'warn', 6.0);
PRAGMA user_version = 1;
"""


def test_v1_archive_is_migrated_without_losing_flights(tmp_path):
    """0.21 archives (schema 1) upgrade in place instead of needing a wipe."""
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_V1_SCHEMA)
    conn.close()

    store = ArchiveWriter(path)
    archive = ArchiveReader(store.path)
    assert store.ping() is True
    old = archive.get_flight("old111-5")
    assert old is not None and old["callsign"] == "OLD1"
    assert old["registration"] == ""
    point = archive.get_telemetry("old111-5")[0]
    assert point["latitude"] == 35.0
    assert "integrity_status" not in point  # unknown for pre-0.22 samples
    assert archive.get_alerts(flight_id="old111-5")[0]["rule"] == "warn"
    assert store.finalize_plane(one_fix_plane("new222", 100.0), alerts=[]) is True
    assert store.write_frames([RawFrame(hex="5d4ca2d4e3d2a7", timestamp=1.0)]) is True
    archive.close()
    store.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    conn.close()


# --- gnss_height in the archive (schema 3) -----------------------------------

_V2_TELEMETRY = """
CREATE TABLE flights (
    flight_id TEXT PRIMARY KEY, icao TEXT NOT NULL, status TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL,
    retained INTEGER NOT NULL DEFAULT 1, callsign TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '', aircraft_type TEXT NOT NULL DEFAULT '',
    registration TEXT NOT NULL DEFAULT '', info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE telemetry (
    flight_id TEXT NOT NULL, timestamp REAL NOT NULL, icao TEXT NOT NULL,
    latitude REAL, longitude REAL, altitude REAL, speed REAL, heading REAL,
    {receivers}
    integrity_score REAL, integrity_status TEXT, integrity_flags TEXT,
    PRIMARY KEY (flight_id, timestamp)
);
CREATE TABLE integrity (
    flight_id TEXT NOT NULL, timestamp REAL NOT NULL, icao TEXT NOT NULL,
    score REAL NOT NULL, status TEXT NOT NULL, flags TEXT NOT NULL DEFAULT '[]',
    checks TEXT, PRIMARY KEY (flight_id, timestamp)
);
CREATE TABLE frames (
    timestamp REAL NOT NULL, receiver TEXT NOT NULL DEFAULT '', hex TEXT NOT NULL,
    df INTEGER, icao TEXT, rssi REAL, clock INTEGER, bearing REAL,
    elevation REAL, cfo REAL
);
CREATE TABLE alerts (
    alert_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, icao TEXT, callsign TEXT,
    zone TEXT, rule TEXT, active INTEGER, activated_at REAL, deactivated_at REAL,
    eta REAL, reason TEXT, last_updated REAL, latitude REAL, longitude REAL,
    altitude REAL
);
INSERT INTO flights (flight_id, icao, status, start_time, end_time, callsign)
    VALUES ('old222-5', 'old222', 'completed', 5.0, 50.0, 'OLD2');
INSERT INTO telemetry (flight_id, timestamp, icao, latitude, longitude, altitude,
    integrity_status) VALUES ('old222-5', 5.0, 'old222', 35.0, -78.0, 3000.0, 'ok');
PRAGMA user_version = 2;
"""


def _v2_archive(path, *, receivers: bool = True) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            _V2_TELEMETRY.replace("{receivers}", "receivers TEXT," if receivers else "")
        )
    conn.close()


def _columns(path) -> tuple[int, set[str]]:
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        cols = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)")}
    conn.close()
    return version, cols


def test_v2_archive_is_migrated_to_v3_keeping_data(tmp_path):
    path = tmp_path / "pyaerial.db"
    _v2_archive(path)
    store = ArchiveWriter(path)
    archive = ArchiveReader(store.path)
    assert store.ping() is True
    version, cols = _columns(path)
    assert version == 3 and "gnss_height" in cols
    old = archive.get_telemetry("old222-5")
    assert old[0]["altitude"] == 3000.0 and old[0]["integrity_status"] == "ok"
    assert "gnss_height" not in old[0]  # unknown for pre-v3 samples
    assert store.finalize_plane(gnss_then_baro_plane(100.0), alerts=[]) is True
    assert archive.get_telemetry("abc123-100")[0]["gnss_height"] == 10972.8
    archive.close()
    store.close()


def test_v1_archive_is_migrated_straight_to_v3(tmp_path):
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_V1_SCHEMA)
    conn.close()
    store = ArchiveWriter(path)
    archive = ArchiveReader(store.path)
    version, cols = _columns(path)
    assert version == 3
    assert {"receivers", "integrity_status", "gnss_height"} <= cols
    assert archive.get_flight("old111-5")["callsign"] == "OLD1"
    assert store.finalize_plane(gnss_then_baro_plane(100.0), alerts=[]) is True
    archive.close()
    store.close()


def test_v2_archive_missing_only_later_columns_is_upgraded(tmp_path):
    """0.22.0 stamped unversioned archives v2 without adding v2 columns."""
    path = tmp_path / "pyaerial.db"
    _v2_archive(path, receivers=False)
    store = ArchiveWriter(path)
    assert store.ping() is True
    version, cols = _columns(path)
    assert version == 3 and {"receivers", "gnss_height"} <= cols
    store.close()


def test_unrecognised_v2_archive_is_refused_untouched(tmp_path, caplog):
    path = tmp_path / "pyaerial.db"
    _v2_archive(path)
    with sqlite3.connect(path) as conn:  # claims v2 but lacks a v1 column
        conn.execute("ALTER TABLE telemetry DROP COLUMN latitude")
    conn.close()
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store = ArchiveWriter(path)
    assert store.ping() is False
    assert "not a PyAerial archive" in caplog.text
    assert "its telemetry table has other columns" in caplog.text
    version, cols = _columns(path)
    assert version == 2 and "gnss_height" not in cols  # rolled back
    store.close()


# --- Nullable integrity score ------------------------------------------------


_V2_INTEGRITY = """
CREATE TABLE flights (
    flight_id TEXT PRIMARY KEY, icao TEXT NOT NULL, status TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL,
    retained INTEGER NOT NULL DEFAULT 1, callsign TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '', aircraft_type TEXT NOT NULL DEFAULT '',
    registration TEXT NOT NULL DEFAULT '', info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE telemetry (
    flight_id TEXT NOT NULL, timestamp REAL NOT NULL, icao TEXT NOT NULL,
    latitude REAL, longitude REAL, altitude REAL, speed REAL, heading REAL,
    receivers TEXT, integrity_score REAL, integrity_status TEXT, integrity_flags TEXT,
    PRIMARY KEY (flight_id, timestamp)
);
CREATE TABLE integrity (
    flight_id TEXT NOT NULL, timestamp REAL NOT NULL, icao TEXT NOT NULL,
    score REAL NOT NULL, status TEXT NOT NULL, flags TEXT NOT NULL DEFAULT '[]',
    checks TEXT, PRIMARY KEY (flight_id, timestamp)
);
CREATE INDEX idx_integrity_icao_ts ON integrity(icao, timestamp);
CREATE TABLE alerts (
    alert_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, icao TEXT, callsign TEXT,
    zone TEXT, rule TEXT, active INTEGER, activated_at REAL, deactivated_at REAL,
    eta REAL, reason TEXT, last_updated REAL, latitude REAL, longitude REAL,
    altitude REAL
);
INSERT INTO integrity VALUES ('old222-5', 6.0, 'old222', 0.3, 'spoofed', '["aoa"]', '{}');
INSERT INTO integrity VALUES ('old222-5', 9.0, 'old222', 0.95, 'ok', '[]', NULL);
PRAGMA user_version = {version};
"""


def _integrity_info(path) -> tuple[int, dict[str, int], list, list]:
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        notnull = {
            row[1]: row[3] for row in conn.execute("PRAGMA table_info(integrity)")
        }
        rows = conn.execute(
            "SELECT flight_id, timestamp, score, status, flags FROM integrity "
            "ORDER BY timestamp"
        ).fetchall()
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'integrity' AND name NOT LIKE 'sqlite_autoindex%'"
            )
        ]
    conn.close()
    return version, notnull, rows, indexes


@pytest.mark.parametrize("version", [2, 3])
def test_not_null_integrity_score_is_rebuilt_keeping_rows_and_indexes(
    tmp_path, version
):
    """v2 (and pre-release v3 files from this cycle) declared score NOT NULL."""
    path = tmp_path / "pyaerial.db"
    script = _V2_INTEGRITY.replace("{version}", str(version))
    if version == 3:
        script = script.replace(
            "integrity_flags TEXT,\n", "integrity_flags TEXT, gnss_height REAL,\n"
        )
    with sqlite3.connect(path) as conn:
        conn.executescript(script)
    conn.close()

    store = ArchiveWriter(path)
    assert store.ping() is True
    new_version, notnull, rows, indexes = _integrity_info(path)
    assert new_version == 3
    assert notnull["score"] == 0 and notnull["status"] == 1
    assert rows == [
        ("old222-5", 6.0, 0.3, "spoofed", '["aoa"]'),
        ("old222-5", 9.0, 0.95, "ok", "[]"),
    ]
    assert indexes == ["idx_integrity_icao_ts"]
    assert store.finalize_plane(with_unknown_then_ok(track_plane()), alerts=[]) is True
    store.close()


def test_integrity_rebuild_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_V2_INTEGRITY.replace("{version}", "2"))
    conn.close()
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(schema, "rebuild_integrity", boom)
    store = ArchiveWriter(path)
    assert store.ping() is False
    version, notnull, rows, _ = _integrity_info(path)
    assert version == 2 and notnull["score"] == 1 and len(rows) == 2
    with sqlite3.connect(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)")}
    conn.close()
    assert "gnss_height" not in cols  # the column adds rolled back too
    store.close()


# --- Unversioned (0.12/0.13) archives ----------------------------------------

_V013 = """
CREATE TABLE flights (
    flight_id TEXT PRIMARY KEY, icao TEXT NOT NULL, status TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL,
    retained INTEGER NOT NULL DEFAULT 1, callsign TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '', aircraft_type TEXT NOT NULL DEFAULT '',
    registration TEXT NOT NULL DEFAULT '', info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE telemetry (
    flight_id TEXT NOT NULL, timestamp REAL NOT NULL, icao TEXT NOT NULL,
    latitude REAL, longitude REAL, altitude REAL, speed REAL, heading REAL,
    PRIMARY KEY (flight_id, timestamp)
);
CREATE TABLE alerts (
    alert_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, icao TEXT, callsign TEXT,
    zone TEXT, rule TEXT, active INTEGER, activated_at REAL, deactivated_at REAL,
    eta REAL, reason TEXT, last_updated REAL, latitude REAL, longitude REAL,
    altitude REAL
);
INSERT INTO flights (flight_id, icao, status, start_time, end_time, callsign)
    VALUES ('old013-5', 'old013', 'completed', 5.0, 50.0, 'OLD13');
INSERT INTO telemetry (flight_id, timestamp, icao, latitude, longitude)
    VALUES ('old013-5', 5.0, 'old013', 35.0, -78.0);
"""


def _version_and_tables(path) -> tuple[int, set[str]]:
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    conn.close()
    return version, tables


def test_unversioned_013_archive_is_migrated(tmp_path):
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_V013)
    conn.close()
    store = ArchiveWriter(path)
    archive = ArchiveReader(store.path)
    assert store.ping() is True
    assert archive.get_flight("old013-5")["callsign"] == "OLD13"
    assert store.finalize_plane(fine_track_plane("new333", 1_790_000_000.0), alerts=[]) is True
    version, tables = _version_and_tables(path)
    assert version == 3 and {"integrity", "frames"} <= tables
    archive.close()
    store.close()


@pytest.mark.parametrize(
    "script",
    [
        "CREATE TABLE notes (x TEXT); INSERT INTO notes VALUES ('mine');",
        "CREATE TABLE flights (id INTEGER PRIMARY KEY, name TEXT);",
    ],
)
def test_unversioned_foreign_database_is_refused_untouched(tmp_path, caplog, script):
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(script)
    conn.close()
    before = _version_and_tables(path)
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store = ArchiveWriter(path)
    assert store.ping() is False
    assert _version_and_tables(path) == before  # nothing stamped or created
    assert "not a PyAerial archive" in caplog.text
    store.close()


def test_portal_reports_a_missing_or_outdated_archive(tmp_path, caplog):
    missing = tmp_path / "nope" / "pyaerial.db"
    with caplog.at_level(logging.WARNING, logger="pyaerial.store"):
        portal = ArchiveReader(missing)
    assert portal.ping() is False
    assert portal.state == "missing"
    assert not missing.parent.exists()  # created nothing
    assert "not been created" in caplog.text

    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.executescript(_V013 + "PRAGMA user_version = 1;")
    conn.close()
    portal_old = ArchiveReader(old)
    assert portal_old.ping() is False and portal_old.state == "needs_upgrade"
    assert _version_and_tables(old)[0] == 1  # left for the engine to upgrade
    portal.close()
    portal_old.close()


def test_newer_schema_says_upgrade_not_reset(tmp_path, caplog):
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_V013 + "PRAGMA user_version = 9;")
    conn.close()
    with caplog.at_level(logging.ERROR, logger="pyaerial.store"):
        store = ArchiveWriter(path)
    assert store.ping() is False
    assert "newer PyAerial" in caplog.text
    assert "Upgrade PyAerial" in caplog.text
    assert "reset" not in caplog.text
    store.close()


def test_reset_prompt_explains_a_newer_archive(tmp_path, capsys, monkeypatch):
    from pyaerial.view.reset import reset_history

    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_V013 + "PRAGMA user_version = 9;")
    conn.close()
    store = ArchiveWriter(path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert reset_history(store, None) == 1
    assert "newer PyAerial" in capsys.readouterr().out
    assert _version_and_tables(path)[0] == 9  # untouched
    store.close()


# --- 0.13 archives that 0.22.0 stamped v2 ------------------------------------

_V013_STAMPED = """
CREATE TABLE flights (
    flight_id TEXT PRIMARY KEY, icao TEXT NOT NULL, status TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL,
    retained INTEGER NOT NULL DEFAULT 1, callsign TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '', aircraft_type TEXT NOT NULL DEFAULT '',
    {registration} info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE telemetry (
    flight_id TEXT NOT NULL, timestamp REAL NOT NULL, icao TEXT NOT NULL,
    latitude REAL, longitude REAL, altitude REAL, speed REAL, heading REAL,
    PRIMARY KEY (flight_id, timestamp)
);
CREATE TABLE integrity (
    flight_id TEXT NOT NULL, timestamp REAL NOT NULL, icao TEXT NOT NULL,
    score REAL NOT NULL, status TEXT NOT NULL, flags TEXT NOT NULL DEFAULT '[]',
    checks TEXT, PRIMARY KEY (flight_id, timestamp)
);
CREATE TABLE alerts (
    alert_id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, icao TEXT, callsign TEXT,
    zone TEXT, rule TEXT, active INTEGER, activated_at REAL, deactivated_at REAL,
    eta REAL, reason TEXT, last_updated REAL, latitude REAL, longitude REAL,
    altitude REAL
);
INSERT INTO flights (flight_id, icao, status, start_time, end_time)
    VALUES ('abc-1', 'abc', 'completed', 1, 100);
PRAGMA user_version = {version};
"""


@pytest.mark.parametrize(
    ("version", "registration"),
    [(2, "registration TEXT NOT NULL DEFAULT '',"), (3, "")],
)
def test_stamped_archive_missing_later_columns_is_upgraded(
    tmp_path, version, registration
):
    path = tmp_path / "pyaerial.db"
    script = _V013_STAMPED.replace("{version}", str(version)).replace(
        "{registration}", registration
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(script)
    conn.close()
    store = ArchiveWriter(path)
    archive = ArchiveReader(store.path)
    assert store.state == "ok"
    assert archive.get_flight("abc-1") is not None
    with sqlite3.connect(path) as conn:
        telemetry = {r[1] for r in conn.execute("PRAGMA table_info(telemetry)")}
        flights = {r[1] for r in conn.execute("PRAGMA table_info(flights)")}
        version_now = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert {"receivers", "integrity_status", "gnss_height"} <= telemetry
    assert "registration" in flights and version_now == 3
    assert store.finalize_plane(pad_plane(1_790_000_000.0, samples=3), alerts=[]) is True
    archive.close()
    store.close()


def test_portal_reports_missing_columns_as_needing_upgrade(tmp_path):
    path = tmp_path / "pyaerial.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            _V013_STAMPED.replace("{version}", "3").replace("{registration}", "")
        )
    conn.close()
    portal = ArchiveReader(path)
    assert portal.state == "needs_upgrade"
    portal.close()
