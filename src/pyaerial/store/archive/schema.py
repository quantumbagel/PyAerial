"""Archive schema: DDL, version detection, and in-place upgrades.

Plain functions over a :class:`sqlite3.Connection`; ``path`` only names the
file in messages. The writer runs the DDL (:func:`ensure_schema`,
:func:`drop_tables`); the read-only reader only inspects
(:func:`detect_version`, :func:`schema_current`).
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("pyaerial.store")

SCHEMA_VERSION = 3
FLIGHT_STATUS_COMPLETED = "completed"
FLIGHT_STATUS_LIVE = "live"
# Columns a schema 1 (PyAerial 0.12-0.21) archive must have to be upgraded.
_V1_COLUMNS = {
    "flights": {
        "flight_id",
        "icao",
        "status",
        "start_time",
        "end_time",
        "retained",
        "callsign",
        "model",
        "owner",
        "country",
        "aircraft_type",
        "info",
    },
    "telemetry": {
        "flight_id",
        "timestamp",
        "icao",
        "latitude",
        "longitude",
        "altitude",
        "speed",
        "heading",
    },
    "alerts": {
        "alert_id",
        "flight_id",
        "icao",
        "callsign",
        "zone",
        "rule",
        "active",
        "activated_at",
        "deactivated_at",
        "eta",
        "reason",
        "last_updated",
        "latitude",
        "longitude",
        "altitude",
    },
}
# Schema version -> (table, column, definition) it added. Each only adds
# nullable/defaulted columns, so older archives upgrade in place.
_ADDED_COLUMNS: dict[int, tuple[tuple[str, str, str], ...]] = {
    2: (
        ("flights", "registration", "TEXT NOT NULL DEFAULT ''"),
        ("telemetry", "receivers", "TEXT"),
        ("telemetry", "integrity_score", "REAL"),
        ("telemetry", "integrity_status", "TEXT"),
        ("telemetry", "integrity_flags", "TEXT"),
    ),
    # Metres above the WGS84 ellipsoid from TC 20-22 positions; altitude is
    # barometric only.
    3: (("telemetry", "gnss_height", "REAL"),),
}
# Every integrity verdict change; checks is the full JSON result. score is
# NULL for an ``unknown`` verdict (no check had evidence). v2 declared it
# NOT NULL, which SQLite can only drop by rebuilding the table.
_INTEGRITY_COLUMNS = (
    "flight_id",
    "timestamp",
    "icao",
    "score",
    "status",
    "flags",
    "checks",
)
_INTEGRITY_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    flight_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    icao TEXT NOT NULL,
    score REAL,
    status TEXT NOT NULL,
    flags TEXT NOT NULL DEFAULT '[]',
    checks TEXT,
    PRIMARY KEY (flight_id, timestamp)
)"""
_INTEGRITY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_integrity_icao_ts ON integrity(icao, timestamp)"
)
# The current schema, one statement each so a new archive or an upgrade runs
# them inside one transaction (executescript would commit part way).
_SCHEMA_DDL = (
    """CREATE TABLE IF NOT EXISTS flights (
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
        registration TEXT NOT NULL DEFAULT '',
        info TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_flights_end_time ON flights(end_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_flights_icao ON flights(icao)",
    "CREATE INDEX IF NOT EXISTS idx_flights_callsign ON flights(callsign)",
    # receivers / integrity_flags are JSON arrays; integrity_* is the verdict
    # in effect when the sample arrived (NULL before the first, score NULL for
    # unknown). altitude is barometric; gnss_height is metres above the WGS84
    # ellipsoid (TC 20-22 positions), NULL when not sent.
    """CREATE TABLE IF NOT EXISTS telemetry (
        flight_id TEXT NOT NULL,
        timestamp REAL NOT NULL,
        icao TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        altitude REAL,
        speed REAL,
        heading REAL,
        receivers TEXT,
        integrity_score REAL,
        integrity_status TEXT,
        integrity_flags TEXT,
        gnss_height REAL,
        PRIMARY KEY (flight_id, timestamp)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_telemetry_icao_ts ON telemetry(icao, timestamp)",
    _INTEGRITY_TABLE.format(name="integrity"),
    _INTEGRITY_INDEX,
    # One row per receiver copy of every frame (archive.raw_frames).
    """CREATE TABLE IF NOT EXISTS frames (
        timestamp REAL NOT NULL,
        receiver TEXT NOT NULL DEFAULT '',
        hex TEXT NOT NULL,
        df INTEGER,
        icao TEXT,
        rssi REAL,
        clock INTEGER,
        bearing REAL,
        elevation REAL,
        cfo REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_frames_icao_ts ON frames(icao, timestamp)",
    """CREATE TABLE IF NOT EXISTS alerts (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_alerts_flight ON alerts(flight_id)",
    "CREATE INDEX IF NOT EXISTS idx_alerts_activated ON alerts(activated_at DESC)",
)
_SCHEMA_TABLES = ("flights", "telemetry", "integrity", "frames", "alerts")


class HistorySchemaMismatch(RuntimeError):
    """The archive file was written by an incompatible PyAerial version."""


def schema_mismatch(path: str, version: int, detail: str = "") -> HistorySchemaMismatch:
    """A file this PyAerial cannot use. Newer schemas must not be reset."""
    if version > SCHEMA_VERSION:
        return HistorySchemaMismatch(
            f"History database {path} was written by a newer PyAerial "
            f"(schema version {version}; this one supports up to "
            f"{SCHEMA_VERSION}). Upgrade PyAerial, or restore a backup taken "
            "before the upgrade. The archive is left untouched."
        )
    return HistorySchemaMismatch(
        f"History database {path} is not a PyAerial archive{detail}. "
        "Point database.path at another file, or move this one aside "
        "(`pyaerial reset --yes` deletes it and starts a new archive)."
    )


def detect_version(conn: sqlite3.Connection, path: str) -> int:
    """The archive's schema version; 0 for an empty (new) file.

    0.12/0.13 archives never set ``user_version``: an unversioned file
    with a ``flights`` table is treated as version 1 (its columns are
    checked by the upgrade). Any other unversioned file with tables is
    someone else's database and is never written to.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise schema_mismatch(path, version)
    if version:
        return version
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        return 0
    if "flights" in tables:
        return 1
    raise schema_mismatch(
        path, 0, f" (no schema version; tables: {', '.join(sorted(tables))})"
    )


def schema_current(conn: sqlite3.Connection, path: str) -> bool:
    """Read-only check: no DDL (and no write lock) is needed to open it."""
    return (
        detect_version(conn, path) == SCHEMA_VERSION
        and not integrity_score_required(conn)
        and not schema_gaps(conn)
    )


def schema_gaps(conn: sqlite3.Connection) -> bool:
    """Whether a table or a later (nullable) column is missing.

    The version stamp alone is not proof: 0.22.0 stamped 0.12/0.13
    archives as v2 without adding the v2 columns.
    """
    columns = table_columns(conn)
    if any(not columns[table] for table in _SCHEMA_TABLES):
        return True
    return any(
        column not in columns[table]
        for added in _ADDED_COLUMNS.values()
        for table, column, _definition in added
    )


def table_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    return {
        table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for table in _SCHEMA_TABLES
    }


def integrity_score_required(conn: sqlite3.Connection) -> bool:
    """True for a v2 (or pre-release v3) ``integrity.score NOT NULL``."""
    return any(
        row[1] == "score" and row[3]
        for row in conn.execute("PRAGMA table_info(integrity)")
    )


def ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    # A current archive needs no DDL, so opening it never queues behind
    # the engine's write lock (it took ~5 s with busy_timeout before).
    if not schema_current(conn, path):
        upgrade(conn, path)


def upgrade(conn: sqlite3.Connection, path: str) -> None:
    """Create a new archive or upgrade an older one in place, keeping every flight.

    Each version since 1 added columns and tables. Samples archived before
    a column existed read back without that field: no receivers or verdict
    before v2, no GNSS height before v3. v3 also lets ``integrity.score``
    be NULL, which needs a table rebuild. One transaction, version stamped
    last: a file that is not a PyAerial archive is refused and left
    untouched, and a failure part way leaves the file as it was.
    """
    # IMMEDIATE: an engine and a reset opening the file together must not
    # both upgrade it. The version is re-read under the lock.
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = detect_version(conn, path)
        if version:
            add_columns(conn, path, version)
        if version and integrity_score_required(conn):
            rebuild_integrity(conn)
        for statement in _SCHEMA_DDL:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if version and version != SCHEMA_VERSION:
        log.info(
            "Upgraded history database %s from schema version %d to %d",
            path,
            version,
            SCHEMA_VERSION,
        )


def add_columns(conn: sqlite3.Connection, path: str, version: int) -> None:
    """Check the v1 core, then add every later column that is missing.

    Only the v1 columns are required, whatever version the file claims:
    later columns are nullable or defaulted, and 0.22.0 stamped 0.12/0.13
    archives v2 without adding them. An existing ``integrity`` table must
    have its columns (the score rebuild copies them).
    """
    columns = table_columns(conn)
    required = {table: set(cols) for table, cols in _V1_COLUMNS.items()}
    if columns["integrity"]:
        required["integrity"] = set(_INTEGRITY_COLUMNS)
    for table, needed in required.items():
        if not needed <= columns[table]:
            raise schema_mismatch(
                path,
                version,
                f" (it claims schema version {version}, but its {table} "
                "table has other columns)",
            )
    for added in _ADDED_COLUMNS.values():
        for table, column, definition in added:
            if column not in columns[table]:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rebuild_integrity(conn: sqlite3.Connection) -> None:
    """Recreate ``integrity`` without NOT NULL on score, keeping every row.

    SQLite's documented table-rebuild procedure (ALTER TABLE cannot drop
    a constraint); runs inside the caller's transaction. The table has no
    triggers, views, or foreign keys referring to it.
    """
    cols = ", ".join(_INTEGRITY_COLUMNS)
    conn.execute(_INTEGRITY_TABLE.format(name="integrity_v3"))
    conn.execute(f"INSERT INTO integrity_v3 ({cols}) SELECT {cols} FROM integrity")
    conn.execute("DROP TABLE integrity")  # drops its index too
    conn.execute("ALTER TABLE integrity_v3 RENAME TO integrity")
    conn.execute(_INTEGRITY_INDEX)


def drop_tables(conn: sqlite3.Connection) -> None:
    with conn:
        for table in ("alerts", "frames", "integrity", "telemetry", "flights"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version = 0")
