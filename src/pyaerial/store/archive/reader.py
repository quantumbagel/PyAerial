"""Read-only archive access for the portal: never creates, upgrades, or
locks the file, and reports why history is unavailable through ``state``."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

from pyaerial.store.archive._conn import (
    ArchiveConnection,
    HistoryUnavailable,
    unavailable,
)
from pyaerial.store.archive.rows import (
    alert_from_row,
    flight_from_row,
    telemetry_from_row,
)
from pyaerial.store.archive.schema import (
    FLIGHT_STATUS_COMPLETED,
    FLIGHT_STATUS_LIVE,
    HistorySchemaMismatch,
    detect_version,
    schema_current,
)

log = logging.getLogger("pyaerial.store")


def _contains_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class ArchiveReader(ArchiveConnection):
    """The portal's view of the archive, opened with ``mode=ro``.

    No schema work, no write lock, nothing created. An archive the engine
    has not created or upgraded yet is reported through :attr:`state`
    (``missing`` / ``needs_upgrade``; ``incompatible`` or ``down`` for a
    foreign file or an I/O error) and retried.
    """

    read_only: ClassVar[bool] = True

    def __init__(self, path: str | Path):
        super().__init__(path)

    def _connect(self) -> None:
        """Open for reading only; never creates, upgrades, or locks the file."""
        if not Path(self.path).is_file():
            self._report(
                "missing",
                f"History archive {self.path} has not been created yet; the "
                "engine (`pyaerial run`) creates it. History is empty until then.",
                level=logging.WARNING,
            )
            return
        try:
            conn = sqlite3.connect(
                f"{Path(self.path).as_uri()}?mode=ro", uri=True, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                if detect_version(conn, self.path) == 0:
                    conn.close()
                    self._report(
                        "missing",
                        f"History archive {self.path} is empty; the engine "
                        "creates its tables on its next start.",
                        level=logging.WARNING,
                    )
                    return
                if not schema_current(conn, self.path):
                    conn.close()
                    self._report(
                        "needs_upgrade",
                        f"History archive {self.path} needs an upgrade by the "
                        "engine; start `pyaerial run` with this PyAerial. "
                        "History is unavailable until then.",
                        level=logging.WARNING,
                    )
                    return
            except BaseException:
                conn.close()
                raise
            self._opened(conn)
        except HistorySchemaMismatch as exc:
            self._report("incompatible", str(exc))
        except sqlite3.Error as exc:
            self._report(
                "down", f"History database unavailable at {self.path} ({exc})."
            )

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
        params: list[Any] = [FLIGHT_STATUS_LIVE]
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
            return [flight_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise unavailable("flights", exc) from exc

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        try:
            with self._session("flight") as conn:
                row = conn.execute(
                    "SELECT * FROM flights WHERE flight_id = ?", (flight_id,)
                ).fetchone()
            return flight_from_row(row) if row else None
        except sqlite3.Error as exc:
            raise unavailable("flight", exc) from exc

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
        sql += (
            " ORDER BY timestamp DESC"
            if limited and newest
            else " ORDER BY timestamp ASC"
        )
        if limited and limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        if limited and newest:
            sql = f"SELECT * FROM ({sql}) ORDER BY timestamp ASC"
        try:
            with self._session("telemetry") as conn:
                rows = conn.execute(sql, params).fetchall()
            return [telemetry_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise unavailable("telemetry", exc) from exc

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
            return {row["flight_id"]: telemetry_from_row(row) for row in rows}
        except sqlite3.Error as exc:
            raise unavailable("latest telemetry", exc) from exc

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
            return [alert_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise unavailable("alerts", exc) from exc

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
            return [alert_from_row(row) for row in rows]
        except sqlite3.Error as exc:
            raise unavailable("alert stats", exc) from exc

    def count_flights(self) -> int:
        try:
            with self._session("query") as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM flights "
                    "WHERE status != ? AND retained = 1",
                    (FLIGHT_STATUS_LIVE,),
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
            return [flight_from_row(row) for row in rows]
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
                    (flight_id, FLIGHT_STATUS_COMPLETED),
                ).fetchone()
            return row is not None
        except (sqlite3.Error, HistoryUnavailable):
            return False

    def data_size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.path + suffix)
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total
