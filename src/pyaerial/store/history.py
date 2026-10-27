"""SQLite research archive: flights, labelled telemetry, integrity verdicts,
alert episodes, and (optionally) every raw receiver frame."""

from __future__ import annotations

import contextlib
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from shapely import Polygon

from pyaerial.alerts.retain import should_retain
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTEGRITY_LOG,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import (
    Verdict,
    flight_id_for_plane,
    iter_telemetry_samples,
    sample_provenance,
)
from pyaerial.receivers.frames import RawFrame, frame_fields

log = logging.getLogger("pyaerial.store")

_RECONNECT_DELAY = 2.0
_SCHEMA_VERSION = 2
# Rows deleted per prune_frames call, so one prune never stalls the engine tick.
_FRAME_PRUNE_BATCH = 100_000
# Telemetry columns stored as JSON arrays.
_JSON_LIST_COLUMNS = ("receivers", "integrity_flags")
_FLIGHT_STATUS_COMPLETED = "completed"
_FLIGHT_STATUS_LIVE = "live"


class HistoryUnavailable(RuntimeError):
    """Configured SQLite archive cannot be read."""


class HistorySchemaMismatch(RuntimeError):
    """The archive file was written by an incompatible PyAerial version."""


def _unavailable(action: str, exc: sqlite3.Error | None = None) -> HistoryUnavailable:
    if exc is not None:
        log.warning("History database unavailable for %s: %s", action, exc)
        return HistoryUnavailable(f"history archive is unavailable ({action}): {exc}")
    return HistoryUnavailable(f"history archive is unavailable ({action})")


def _bind_eta(eta: Any) -> float | None:
    if eta is None:
        return None
    try:
        value = float(eta)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _bind_reason(reason: Any) -> str | None:
    if reason is None:
        return None
    if isinstance(reason, str):
        return reason
    return json_dumps(reason, default=str)


def _json_list(value: Any) -> str | None:
    return json_dumps(list(value)) if value else None


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def build_telemetry_docs(
    plane: dict, flight_id: str, icao: str
) -> list[dict[str, Any]]:
    """Build telemetry documents from a plane's in-memory time series.

    Each sample carries the receivers that heard it and the integrity verdict
    in effect when it arrived (see :func:`pyaerial.models.sample_provenance`).
    """
    docs: list[dict[str, Any]] = []
    used_ts: set[float] = set()
    for timestamp, lat, lon, alt, speed, heading in iter_telemetry_samples(plane):
        stamp = timestamp
        while stamp in used_ts:
            stamp += 1e-6
        used_ts.add(stamp)
        doc: dict[str, Any] = {
            "flight_id": flight_id,
            "icao": icao,
            "timestamp": stamp,
            "latitude": lat,
            "longitude": lon,
        }
        if alt is not None:
            doc["altitude"] = alt
        if speed is not None:
            doc["speed"] = speed
        if heading is not None:
            doc["heading"] = heading
        doc.update(sample_provenance(plane, timestamp))
        docs.append(doc)
    return docs


def _contains_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _alert_lat_lon(alert: dict[str, Any]) -> tuple[Any, Any]:
    if alert.get("latitude") is not None or alert.get("longitude") is not None:
        return alert.get("latitude"), alert.get("longitude")
    position = alert.get("position") or {}
    coords = position.get("coordinates") or [None, None]
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return coords[1], coords[0]
    return None, None


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class HistoryStore:
    """SQLite writer/reader for the research archive."""

    def __init__(
        self,
        path: str | Path,
        *,
        config: Config | None = None,
        polygons: dict[str, Polygon] | None = None,
        disabled: bool = False,
    ):
        self.path = str(Path(path).expanduser())
        if not Path(self.path).is_absolute():
            self.path = str(Path(self.path).resolve())
        self.config = config
        self.polygons = polygons or {}
        self.disabled = disabled
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_connect_attempt = 0.0
        self._reported_down = False
        self._dropping_frames = False
        if not disabled:
            self._connect()

    def _connect(self, *, recreate: bool = False) -> None:
        try:
            db_path = Path(self.path)
            if db_path.parent:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                if recreate:
                    self._drop_tables(conn)
                self._ensure_schema(conn)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
            if self._reported_down:
                log.info("Reconnected to history database %s", self.path)
            else:
                log.info("Opened history database %s", self.path)
            self._reported_down = False
        except HistorySchemaMismatch as exc:
            if not self._reported_down:
                log.error("%s", exc)
                self._reported_down = True
        except sqlite3.Error:
            if not self._reported_down:
                log.info(
                    "History database unavailable at %s; operating in offline mode.",
                    self.path,
                )
                self._reported_down = True
            self._conn = None

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, _SCHEMA_VERSION):
            raise HistorySchemaMismatch(
                f"History database {self.path} uses schema version {version}; "
                f"this PyAerial needs version {_SCHEMA_VERSION}. Run "
                "`pyaerial reset --yes` (or delete the file) to recreate it."
            )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS flights (
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
            );
            CREATE INDEX IF NOT EXISTS idx_flights_end_time ON flights(end_time DESC);
            CREATE INDEX IF NOT EXISTS idx_flights_icao ON flights(icao);
            CREATE INDEX IF NOT EXISTS idx_flights_callsign ON flights(callsign);

            -- receivers / integrity_flags are JSON arrays; integrity_* is the
            -- verdict in effect when the sample arrived (NULL before the first).
            CREATE TABLE IF NOT EXISTS telemetry (
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
                PRIMARY KEY (flight_id, timestamp)
            );
            CREATE INDEX IF NOT EXISTS idx_telemetry_icao_ts
                ON telemetry(icao, timestamp);

            -- Every integrity verdict change; checks is the full JSON result.
            CREATE TABLE IF NOT EXISTS integrity (
                flight_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                icao TEXT NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL,
                flags TEXT NOT NULL DEFAULT '[]',
                checks TEXT,
                PRIMARY KEY (flight_id, timestamp)
            );
            CREATE INDEX IF NOT EXISTS idx_integrity_icao_ts
                ON integrity(icao, timestamp);

            -- One row per receiver copy of every frame (archive.raw_frames).
            CREATE TABLE IF NOT EXISTS frames (
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
            );
            CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(timestamp);
            CREATE INDEX IF NOT EXISTS idx_frames_icao_ts ON frames(icao, timestamp);

            CREATE TABLE IF NOT EXISTS alerts (
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
            CREATE INDEX IF NOT EXISTS idx_alerts_flight ON alerts(flight_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_activated
                ON alerts(activated_at DESC);
            """
        )
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()

    @staticmethod
    def _drop_tables(conn: sqlite3.Connection) -> None:
        with conn:
            for table in ("alerts", "frames", "integrity", "telemetry", "flights"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("PRAGMA user_version = 0")

    def _reconnect_if_due(self) -> None:
        """Caller holds ``self._lock``."""
        if self._conn is None:
            now = time.monotonic()
            if now - self._last_connect_attempt >= _RECONNECT_DELAY:
                self._last_connect_attempt = now
                self._connect()

    def _ensure_connected(self) -> bool:
        if self.disabled:
            return False
        with self._lock:
            self._reconnect_if_due()
            return self._conn is not None

    @contextlib.contextmanager
    def _session(self, action: str) -> Iterator[sqlite3.Connection]:
        """Yield the connection with the lock held for the whole block.

        Checking for a connection and then using it under a separately
        acquired lock let another thread close it in between. Raises
        :class:`HistoryUnavailable` when there is no connection.
        """
        if self.disabled:
            raise _unavailable(action)
        with self._lock:
            self._reconnect_if_due()
            conn = self._conn
            if conn is None:
                raise _unavailable(action)
            yield conn

    def ping(self) -> bool:
        if self.disabled:
            return False
        with self._lock:
            self._reconnect_if_due()
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.execute("SELECT 1").fetchone()
                return True
            except sqlite3.Error:
                if not self._reported_down:
                    log.info(
                        "Lost history database connection; operating in offline mode."
                    )
                    self._reported_down = True
                self._close_conn()
                return False

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                log.debug("Error closing history database: %s", exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close_conn()

    def finalize_plane(
        self,
        plane: dict,
        *,
        alerts: list[dict[str, Any]] | None = None,
        from_snapshot: bool = False,
    ) -> bool:
        """Persist a completed flight if the archive policy keeps it.

        Returns True when it is safe to drop the live Redis copy: the flight
        was written, was intentionally discarded, or persistence is disabled.
        Returns False when the flight should have been written but SQLite was
        unavailable.

        ``from_snapshot`` marks a crash/orphan reconstruction from the clipped
        live window. Those are persisted whenever they have a position or
        alerts, and a later longer series may upgrade the archive.
        """
        if self.disabled:
            return True
        alert_docs = alerts or []
        if self.config is None:
            log.error("Cannot retain flights without a Config")
            return False
        flight_id = flight_id_for_plane(plane)
        icao = str(plane.get(STORE_INFO, {}).get(STORE_ICAO, "")).lower()
        telemetry_docs = build_telemetry_docs(plane, flight_id, icao)
        retained = should_retain(
            plane,
            alert_docs,
            self.config,
            self.polygons,
            positions=len(telemetry_docs),
        )
        if not retained and from_snapshot:
            recv = plane.get(STORE_RECV_DATA) or {}
            retained = bool(alert_docs) or bool(recv.get(STORE_LAT))
        if not retained:
            log.debug("Discarded flight %s (archive policy)", flight_id)
            return True
        if not self._ensure_connected():
            return False
        if self.has_completed_flight(flight_id):
            existing = self.telemetry_count(flight_id)
            if len(telemetry_docs) <= existing:
                log.debug("Already archived completed flight %s", flight_id)
                return True
            log.info(
                "Upgrading archived flight %s (%d -> %d telemetry points)",
                flight_id,
                existing,
                len(telemetry_docs),
            )
        if self._persist_completed_flight(
            plane, flight_id, alert_docs, telemetry_docs=telemetry_docs
        ):
            log.debug("Retained completed flight %s", flight_id)
            return True
        return False

    def _persist_completed_flight(
        self,
        plane: dict,
        flight_id: str,
        alerts: list[dict[str, Any]],
        *,
        telemetry_docs: list[dict[str, Any]] | None = None,
    ) -> bool:
        info = plane.get(STORE_INFO, {})
        internal = plane[STORE_INTERNAL]
        icao = info[STORE_ICAO].lower()
        if telemetry_docs is None:
            telemetry_docs = build_telemetry_docs(plane, flight_id, icao)
        try:
            with self._session("persist") as conn:
                with conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO flights (
                            flight_id, icao, status, start_time, end_time,
                            retained, callsign, model, owner, country,
                            aircraft_type, registration, info
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            flight_id,
                            icao,
                            _FLIGHT_STATUS_COMPLETED,
                            internal[STORE_FIRST_PACKET],
                            internal[STORE_MOST_RECENT_PACKET],
                            info.get(STORE_CALLSIGN) or "",
                            info.get("model") or "",
                            info.get("owner") or "",
                            info.get("country") or "",
                            info.get("aircraft_type") or "",
                            info.get("registration") or "",
                            json_dumps(
                                {str(k): v for k, v in info.items()}, default=str
                            ),
                        ),
                    )
                    conn.execute(
                        "DELETE FROM telemetry WHERE flight_id = ?", (flight_id,)
                    )
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO telemetry (
                            flight_id, timestamp, icao, latitude, longitude,
                            altitude, speed, heading, receivers,
                            integrity_score, integrity_status, integrity_flags
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                doc["flight_id"],
                                doc["timestamp"],
                                doc["icao"],
                                doc.get("latitude"),
                                doc.get("longitude"),
                                doc.get("altitude"),
                                doc.get("speed"),
                                doc.get("heading"),
                                _json_list(doc.get("receivers")),
                                doc.get("integrity_score"),
                                doc.get("integrity_status"),
                                (
                                    json_dumps(doc["integrity_flags"])
                                    if "integrity_flags" in doc
                                    else None
                                ),
                            )
                            for doc in telemetry_docs
                        ],
                    )
                    conn.execute(
                        "DELETE FROM integrity WHERE flight_id = ?", (flight_id,)
                    )
                    verdicts: list[Verdict] = internal.get(STORE_INTEGRITY_LOG) or []
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO integrity (
                            flight_id, timestamp, icao, score, status, flags, checks
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                flight_id,
                                verdict.time,
                                icao,
                                verdict.score,
                                verdict.status,
                                json_dumps(list(verdict.flagged)),
                                (
                                    json_dumps(verdict.checks)
                                    if verdict.checks is not None
                                    else None
                                ),
                            )
                            for verdict in verdicts
                        ],
                    )
                    conn.execute(
                        "DELETE FROM alerts WHERE flight_id = ?", (flight_id,)
                    )
                    alert_rows = []
                    for alert in alerts:
                        alert_id = (
                            alert.get("alert_id")
                            or f"{flight_id}:{alert.get('zone', '')}:{alert.get('rule', '')}"
                        )
                        lat, lon = _alert_lat_lon(alert)
                        alert_rows.append(
                            (
                                alert_id,
                                flight_id,
                                alert.get("icao", icao),
                                alert.get("callsign") or info.get(STORE_CALLSIGN) or "",
                                alert.get("zone", ""),
                                alert.get("rule", ""),
                                1 if alert.get("active", False) else 0,
                                alert.get("activated_at"),
                                alert.get("deactivated_at"),
                                _bind_eta(alert.get("eta")),
                                _bind_reason(alert.get("reason")),
                                alert.get("last_updated"),
                                lat,
                                lon,
                                alert.get("altitude"),
                            )
                        )
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO alerts (
                            alert_id, flight_id, icao, callsign, zone, rule,
                            active, activated_at, deactivated_at, eta, reason,
                            last_updated, latitude, longitude, altitude
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        alert_rows,
                    )
            return True
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.error("Failed to persist completed flight %s: %s", flight_id, exc)
            return False

    def write_frames(self, frames: list[RawFrame]) -> bool:
        """Append receiver frames to ``frames``. False (frames dropped) when offline."""
        if self.disabled or not frames:
            return True
        rows = []
        for frame in frames:
            fields = frame_fields(frame.hex)
            rows.append(
                (
                    frame.timestamp,
                    frame.receiver,
                    fields["hex"],
                    fields.get("df"),
                    fields.get("icao"),
                    _finite(frame.rssi),
                    frame.clock,
                    _finite(frame.bearing),
                    _finite(frame.elevation),
                    _finite(frame.cfo),
                )
            )
        try:
            with self._session("frames") as conn, conn:
                conn.executemany(
                    """
                    INSERT INTO frames (
                        timestamp, receiver, hex, df, icao, rssi, clock,
                        bearing, elevation, cfo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        except (sqlite3.Error, HistoryUnavailable) as exc:
            if not self._dropping_frames:
                log.warning("Dropping raw frames while history is unavailable: %s", exc)
                self._dropping_frames = True
            return False
        if self._dropping_frames:
            log.info("Raw frame archiving resumed")
            self._dropping_frames = False
        return True

    def prune_frames(self, before: float) -> int:
        """Delete up to one batch of frames older than *before*; returns the count."""
        if self.disabled:
            return 0
        try:
            with self._session("prune frames") as conn, conn:
                cursor = conn.execute(
                    """
                    DELETE FROM frames WHERE rowid IN (
                        SELECT rowid FROM frames WHERE timestamp < ?
                        ORDER BY timestamp LIMIT ?
                    )
                    """,
                    (before, _FRAME_PRUNE_BATCH),
                )
            return cursor.rowcount
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.warning("Could not prune raw frames: %s", exc)
            return 0

    def list_flights(
        self,
        *,
        skip: int = 0,
        limit: int = 50,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["status != ?", "retained = 1"]
        params: list[Any] = [_FLIGHT_STATUS_LIVE]
        if since is not None:
            clauses.append("end_time >= ?")
            params.append(since)
        if until is not None:
            clauses.append("end_time <= ?")
            params.append(until)
        if q:
            pattern = _contains_pattern(q)
            clauses.append(
                "(icao LIKE ? ESCAPE '\\' OR callsign LIKE ? ESCAPE '\\' "
                "OR flight_id LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        sql = (
            f"SELECT * FROM flights WHERE {' AND '.join(clauses)} "
            "ORDER BY end_time DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, skip])
        try:
            with self._session("flights") as conn:
                rows = conn.execute(sql, params).fetchall()
            return [self._flight_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise _unavailable("flights", exc) from exc

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        try:
            with self._session("flight") as conn:
                row = conn.execute(
                    "SELECT * FROM flights WHERE flight_id = ?", (flight_id,)
                ).fetchone()
            return self._flight_from_row(row) if row else None
        except sqlite3.Error as exc:
            raise _unavailable("flight", exc) from exc

    def get_telemetry(
        self,
        flight_id: str,
        *,
        since: float = 0.0,
        limit: int | None = None,
        newest: bool = False,
    ) -> list[dict[str, Any]]:
        """Telemetry for a flight in ascending time order.

        ``limit`` (> 0) returns at most that many points: the oldest ones after
        ``since``, so a caller can page forward with ``since=<last timestamp>``.
        With ``newest=True`` it keeps the most recent ones instead, so a capped
        read still ends at the flight's last position.
        """
        sql = "SELECT * FROM telemetry WHERE flight_id = ?"
        params: list[Any] = [flight_id]
        if since > 0:
            sql += " AND timestamp > ?"
            params.append(since)
        limited = limit is not None and limit > 0
        sql += " ORDER BY timestamp DESC" if limited and newest else " ORDER BY timestamp ASC"
        if limited:
            sql += " LIMIT ?"
            params.append(int(limit))
        if limited and newest:
            sql = f"SELECT * FROM ({sql}) ORDER BY timestamp ASC"
        try:
            with self._session("telemetry") as conn:
                rows = conn.execute(sql, params).fetchall()
            return [self._telemetry_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise _unavailable("telemetry", exc) from exc

    def latest_telemetry(self, flight_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not flight_ids:
            return {}
        placeholders = ",".join("?" * len(flight_ids))
        sql = f"""
            SELECT t.* FROM telemetry t
            INNER JOIN (
                SELECT flight_id, MAX(timestamp) AS ts
                FROM telemetry
                WHERE flight_id IN ({placeholders})
                GROUP BY flight_id
            ) latest
              ON t.flight_id = latest.flight_id AND t.timestamp = latest.ts
        """
        try:
            with self._session("latest telemetry") as conn:
                rows = conn.execute(sql, flight_ids).fetchall()
            return {row["flight_id"]: self._telemetry_from_row(row) for row in rows}
        except sqlite3.Error as exc:
            raise _unavailable("latest telemetry", exc) from exc

    def get_alerts(
        self,
        *,
        since: float = 0.0,
        until: float | None = None,
        flight_id: str | None = None,
        rule: str | None = None,
        q: str | None = None,
        limit: int = 0,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("activated_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("activated_at <= ?")
            params.append(until)
        if flight_id:
            clauses.append("flight_id = ?")
            params.append(flight_id)
        if rule:
            clauses.append("rule = ?")
            params.append(rule)
        if q:
            pattern = _contains_pattern(q)
            clauses.append(
                "(icao LIKE ? ESCAPE '\\' OR callsign LIKE ? ESCAPE '\\' "
                "OR zone LIKE ? ESCAPE '\\' OR rule LIKE ? ESCAPE '\\' "
                "OR flight_id LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])
        sql = "SELECT * FROM alerts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY activated_at DESC"
        if skip:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit if limit else -1, skip])
        elif limit:
            sql += " LIMIT ?"
            params.append(limit)
        try:
            with self._session("alerts") as conn:
                rows = conn.execute(sql, params).fetchall()
            return [self._alert_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise _unavailable("alerts", exc) from exc

    def alerts_for_flights(self, flight_ids: list[str]) -> list[dict[str, Any]]:
        if not flight_ids:
            return []
        placeholders = ",".join("?" * len(flight_ids))
        try:
            with self._session("alert stats") as conn:
                rows = conn.execute(
                    f"SELECT * FROM alerts WHERE flight_id IN ({placeholders})",
                    flight_ids,
                ).fetchall()
            return [self._alert_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise _unavailable("alert stats", exc) from exc

    def count_flights(self) -> int:
        try:
            with self._session("query") as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM flights "
                    "WHERE status != ? AND retained = 1",
                    (_FLIGHT_STATUS_LIVE,),
                ).fetchone()
            return int(row["n"] if row else 0)
        except (sqlite3.Error, HistoryUnavailable):
            return 0

    def count_alerts(self) -> int:
        try:
            with self._session("query") as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
            return int(row["n"] if row else 0)
        except (sqlite3.Error, HistoryUnavailable):
            return 0

    def distinct_icaos(self) -> list[str]:
        try:
            with self._session("query") as conn:
                rows = conn.execute(
                    "SELECT DISTINCT icao FROM flights ORDER BY icao"
                ).fetchall()
            return [row["icao"] for row in rows if row["icao"]]
        except (sqlite3.Error, HistoryUnavailable):
            return []

    def flights_for_icao(self, icao: str) -> list[dict[str, Any]]:
        try:
            with self._session("query") as conn:
                rows = conn.execute(
                    "SELECT * FROM flights WHERE icao = ? ORDER BY start_time",
                    (icao.lower(),),
                ).fetchall()
            return [self._flight_from_row(row) for row in rows]
        except (sqlite3.Error, HistoryUnavailable):
            return []

    def has_icao(self, icao: str) -> bool:
        try:
            with self._session("query") as conn:
                row = conn.execute(
                    "SELECT 1 FROM flights WHERE icao = ? LIMIT 1",
                    (icao.lower(),),
                ).fetchone()
            return row is not None
        except (sqlite3.Error, HistoryUnavailable):
            return False

    def has_flight(self, icao: str, flight_id: str) -> bool:
        try:
            with self._session("query") as conn:
                row = conn.execute(
                    "SELECT 1 FROM flights WHERE icao = ? AND flight_id = ? LIMIT 1",
                    (icao.lower(), flight_id),
                ).fetchone()
            return row is not None
        except (sqlite3.Error, HistoryUnavailable):
            return False

    def telemetry_count(self, flight_id: str) -> int:
        try:
            with self._session("query") as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM telemetry WHERE flight_id = ?",
                    (flight_id,),
                ).fetchone()
            return int(row["n"] if row is not None else 0)
        except (sqlite3.Error, HistoryUnavailable):
            return 0

    def has_completed_flight(self, flight_id: str) -> bool:
        try:
            with self._session("query") as conn:
                row = conn.execute(
                    "SELECT 1 FROM flights WHERE flight_id = ? AND status = ? LIMIT 1",
                    (flight_id, _FLIGHT_STATUS_COMPLETED),
                ).fetchone()
            return row is not None
        except (sqlite3.Error, HistoryUnavailable):
            return False

    def reset_all(self) -> bool:
        """Drop and recreate every table. False when the archive is offline.

        Raises :class:`HistoryUnavailable` with a readable message when SQLite
        refuses (e.g. "database is locked" while the engine is writing).
        """
        if self.disabled:
            return False
        with self._lock:
            if self._conn is None:
                # Also the way out of a schema mismatch, which refuses to open.
                self._connect(recreate=True)
                return self._conn is not None
        try:
            with self._session("reset") as conn:
                self._drop_tables(conn)
                self._ensure_schema(conn)
        except sqlite3.Error as exc:
            raise _unavailable("reset", exc) from exc
        return True

    def delete_icao(self, icao: str) -> bool:
        """Delete every archived flight of ``icao``. False when offline.

        Raises :class:`HistoryUnavailable` when SQLite refuses (e.g. locked).
        """
        if not self._ensure_connected():
            return False
        icao = icao.lower()
        try:
            with self._session("delete") as conn, conn:
                for table in ("alerts", "frames", "integrity", "telemetry", "flights"):
                    conn.execute(f"DELETE FROM {table} WHERE icao = ?", (icao,))
        except sqlite3.Error as exc:
            raise _unavailable("delete", exc) from exc
        return True

    def data_size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.path + suffix)
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _flight_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        raw = _row_dict(row)
        try:
            info = json.loads(raw.get("info") or "{}")
        except json.JSONDecodeError:
            info = {}
        if not isinstance(info, dict):
            info = {}
        return {
            "_id": raw["flight_id"],
            "flight_id": raw["flight_id"],
            "icao": raw["icao"],
            "status": raw["status"],
            "start_time": raw["start_time"],
            "end_time": raw["end_time"],
            "retained": bool(raw["retained"]),
            "callsign": raw["callsign"] or "",
            "model": raw["model"] or "",
            "owner": raw["owner"] or "",
            "country": raw["country"] or "",
            "aircraft_type": raw["aircraft_type"] or "",
            "registration": raw.get("registration") or "",
            "info": info,
            "active_alerts": [],
        }

    def _telemetry_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        doc = {
            key: value for key, value in _row_dict(row).items() if value is not None
        }
        for key in _JSON_LIST_COLUMNS:
            if key in doc:
                doc[key] = json.loads(doc[key])
        return doc

    def _alert_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        raw = _row_dict(row)
        reason = raw["reason"]
        if isinstance(reason, str) and reason[:1] in "{[":
            with contextlib.suppress(json.JSONDecodeError):
                reason = json.loads(reason)
        return {
            "alert_id": raw["alert_id"],
            "flight_id": raw["flight_id"],
            "icao": raw["icao"],
            "callsign": raw["callsign"],
            "zone": raw["zone"],
            "rule": raw["rule"],
            "active": bool(raw["active"]),
            "activated_at": raw["activated_at"],
            "deactivated_at": raw["deactivated_at"],
            "eta": raw["eta"],
            "reason": reason,
            "last_updated": raw["last_updated"],
            "latitude": raw["latitude"],
            "longitude": raw["longitude"],
            "altitude": raw["altitude"],
        }
