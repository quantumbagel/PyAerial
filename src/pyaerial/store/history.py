"""SQLite persistence for retained historical flights, telemetry, and alerts."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
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
    STORE_INTERNAL,
    STORE_MOST_RECENT_PACKET,
)
from pyaerial.models import flight_id_for_plane, iter_telemetry_samples

log = logging.getLogger("pyaerial.store")

_RECONNECT_DELAY = 2.0
_FLIGHT_STATUS_COMPLETED = "completed"
_FLIGHT_STATUS_LIVE = "live"


def build_telemetry_docs(
    plane: dict, flight_id: str, icao: str
) -> list[dict[str, Any]]:
    """Build telemetry documents from a plane's in-memory time series."""
    docs: list[dict[str, Any]] = []
    for timestamp, lat, lon, alt, speed, heading in iter_telemetry_samples(plane):
        doc: dict[str, Any] = {
            "flight_id": flight_id,
            "icao": icao,
            "timestamp": timestamp,
            "latitude": lat,
            "longitude": lon,
        }
        if alt is not None:
            doc["altitude"] = alt
        if speed is not None:
            doc["speed"] = speed
        if heading is not None:
            doc["heading"] = heading
        docs.append(doc)
    return docs


def _contains_pattern(query: str) -> str:
    escaped = (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
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
    """SQLite writer/reader for retained completed flights only."""

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
        if not disabled:
            self._connect()

    def _connect(self) -> None:
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
            self._ensure_schema(conn)
            self._conn = conn
            if self._reported_down:
                log.info("Reconnected to history database %s", self.path)
            else:
                log.info("Opened history database %s", self.path)
            self._reported_down = False
        except sqlite3.Error:
            if not self._reported_down:
                log.info(
                    "History database unavailable at %s; operating in offline mode.",
                    self.path,
                )
                self._reported_down = True
            self._conn = None

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
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

            CREATE TABLE IF NOT EXISTS telemetry (
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
            CREATE INDEX IF NOT EXISTS idx_telemetry_icao_ts
                ON telemetry(icao, timestamp);

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
        conn.commit()

    def _ensure_connected(self) -> bool:
        if self.disabled:
            return False
        with self._lock:
            if self._conn is None:
                now = time.monotonic()
                if now - self._last_connect_attempt >= _RECONNECT_DELAY:
                    self._last_connect_attempt = now
                    self._connect()
            return self._conn is not None

    def ping(self) -> bool:
        if not self._ensure_connected():
            return False
        assert self._conn is not None
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            if not self._reported_down:
                log.info("Lost history database connection; operating in offline mode.")
                self._reported_down = True
            with self._lock:
                self._close_conn()
            return False

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close_conn()

    def finalize_plane(
        self, plane: dict, *, alerts: list[dict[str, Any]] | None = None
    ) -> bool:
        """Persist a completed flight if retention rules are met.

        Returns True when it is safe to drop the live Redis copy: the flight
        was written, was intentionally discarded, or persistence is disabled.
        Returns False when the flight should have been written but SQLite was
        unavailable.
        """
        if self.disabled:
            return True
        alert_docs = alerts or []
        if self.config is None:
            log.error("Cannot retain flights without a Config")
            return False
        retained = should_retain(plane, alert_docs, self.config, self.polygons)
        if not retained:
            log.debug("Discarded uninteresting flight %s", flight_id_for_plane(plane))
            return True
        if not self._ensure_connected():
            return False
        flight_id = flight_id_for_plane(plane)
        if self._persist_completed_flight(plane, flight_id, alert_docs):
            log.debug("Retained completed flight %s", flight_id)
            return True
        return False

    def _persist_completed_flight(
        self, plane: dict, flight_id: str, alerts: list[dict[str, Any]]
    ) -> bool:
        assert self._conn is not None
        info = plane.get(STORE_INFO, {})
        internal = plane[STORE_INTERNAL]
        icao = info[STORE_ICAO].lower()
        telemetry_docs = build_telemetry_docs(plane, flight_id, icao)
        try:
            with self._lock:
                with self._conn:
                    self._conn.execute(
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
                            json.dumps(
                                {str(k): v for k, v in info.items()}, default=str
                            ),
                        ),
                    )
                    self._conn.execute(
                        "DELETE FROM telemetry WHERE flight_id = ?", (flight_id,)
                    )
                    self._conn.executemany(
                        """
                        INSERT INTO telemetry (
                            flight_id, timestamp, icao, latitude, longitude,
                            altitude, speed, heading
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                            )
                            for doc in telemetry_docs
                        ],
                    )
                    self._conn.execute(
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
                                alert.get("callsign")
                                or info.get(STORE_CALLSIGN)
                                or "",
                                alert.get("zone", ""),
                                alert.get("rule", ""),
                                1 if alert.get("active", False) else 0,
                                alert.get("activated_at"),
                                alert.get("deactivated_at"),
                                alert.get("eta"),
                                alert.get("reason"),
                                alert.get("last_updated"),
                                lat,
                                lon,
                                alert.get("altitude"),
                            )
                        )
                    self._conn.executemany(
                        """
                        INSERT INTO alerts (
                            alert_id, flight_id, icao, callsign, zone, rule,
                            active, activated_at, deactivated_at, eta, reason,
                            last_updated, latitude, longitude, altitude
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        alert_rows,
                    )
            return True
        except sqlite3.Error as exc:
            log.error("Failed to persist completed flight %s: %s", flight_id, exc)
            return False

    def list_flights(
        self,
        *,
        skip: int = 0,
        limit: int = 50,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
    ) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            return []
        assert self._conn is not None
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
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            return [self._flight_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning("History database unavailable for flights: %s", exc)
            return []

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        if not self._ensure_connected():
            return None
        assert self._conn is not None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM flights WHERE flight_id = ?", (flight_id,)
                ).fetchone()
            return self._flight_from_row(row) if row else None
        except sqlite3.Error as exc:
            log.warning("History database unavailable for flight %s: %s", flight_id, exc)
            return None

    def get_telemetry(
        self, flight_id: str, *, since: float = 0.0
    ) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            return []
        assert self._conn is not None
        sql = "SELECT * FROM telemetry WHERE flight_id = ?"
        params: list[Any] = [flight_id]
        if since > 0:
            sql += " AND timestamp > ?"
            params.append(since)
        sql += " ORDER BY timestamp ASC"
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            return [self._telemetry_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning(
                "History database unavailable for telemetry %s: %s", flight_id, exc
            )
            return []

    def latest_telemetry(self, flight_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not flight_ids or not self._ensure_connected():
            return {}
        assert self._conn is not None
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
            with self._lock:
                rows = self._conn.execute(sql, flight_ids).fetchall()
            return {row["flight_id"]: self._telemetry_from_row(row) for row in rows}
        except sqlite3.Error as exc:
            log.warning("History database unavailable for latest telemetry: %s", exc)
            return {}

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
        if not self._ensure_connected():
            return []
        assert self._conn is not None
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
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
            return [self._alert_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning("History database unavailable for alerts: %s", exc)
            return []

    def alerts_for_flights(self, flight_ids: list[str]) -> list[dict[str, Any]]:
        if not flight_ids or not self._ensure_connected():
            return []
        assert self._conn is not None
        placeholders = ",".join("?" * len(flight_ids))
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT * FROM alerts WHERE flight_id IN ({placeholders})",
                    flight_ids,
                ).fetchall()
            return [self._alert_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning("History database unavailable for alert stats: %s", exc)
            return []

    def count_flights(self) -> int:
        if not self._ensure_connected():
            return 0
        assert self._conn is not None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM flights "
                    "WHERE status != ? AND retained = 1",
                    (_FLIGHT_STATUS_LIVE,),
                ).fetchone()
            return int(row["n"] if row else 0)
        except sqlite3.Error:
            return 0

    def count_alerts(self) -> int:
        if not self._ensure_connected():
            return 0
        assert self._conn is not None
        try:
            with self._lock:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
            return int(row["n"] if row else 0)
        except sqlite3.Error:
            return 0

    def distinct_icaos(self) -> list[str]:
        if not self._ensure_connected():
            return []
        assert self._conn is not None
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT DISTINCT icao FROM flights ORDER BY icao"
                ).fetchall()
            return [row["icao"] for row in rows if row["icao"]]
        except sqlite3.Error:
            return []

    def flights_for_icao(self, icao: str) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            return []
        assert self._conn is not None
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM flights WHERE icao = ? ORDER BY start_time",
                    (icao.lower(),),
                ).fetchall()
            return [self._flight_from_row(row) for row in rows]
        except sqlite3.Error:
            return []

    def has_icao(self, icao: str) -> bool:
        if not self._ensure_connected():
            return False
        assert self._conn is not None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM flights WHERE icao = ? LIMIT 1",
                    (icao.lower(),),
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def has_flight(self, icao: str, flight_id: str) -> bool:
        if not self._ensure_connected():
            return False
        assert self._conn is not None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM flights WHERE icao = ? AND flight_id = ? LIMIT 1",
                    (icao.lower(), flight_id),
                ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def reset_all(self) -> None:
        if not self._ensure_connected():
            return
        assert self._conn is not None
        with self._lock:
            with self._conn:
                self._conn.execute("DROP TABLE IF EXISTS alerts")
                self._conn.execute("DROP TABLE IF EXISTS telemetry")
                self._conn.execute("DROP TABLE IF EXISTS flights")
            self._ensure_schema(self._conn)

    def delete_icao(self, icao: str) -> None:
        if not self._ensure_connected():
            return
        assert self._conn is not None
        icao = icao.lower()
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM alerts WHERE icao = ?", (icao,))
                self._conn.execute("DELETE FROM telemetry WHERE icao = ?", (icao,))
                self._conn.execute("DELETE FROM flights WHERE icao = ?", (icao,))

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
        doc = _row_dict(row)
        return {key: value for key, value in doc.items() if value is not None}

    def _alert_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        raw = _row_dict(row)
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
            "reason": raw["reason"],
            "last_updated": raw["last_updated"],
            "latitude": raw["latitude"],
            "longitude": raw["longitude"],
            "altitude": raw["altitude"],
        }
