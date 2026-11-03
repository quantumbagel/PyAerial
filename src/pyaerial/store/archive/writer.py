"""Archive writer for the engine and ``pyaerial reset``: creates and upgrades
the schema, writes finished flights and raw frames, prunes and deletes."""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import Plane
from pyaerial.receivers.frames import RawFrame, frame_fields
from pyaerial.store.archive._conn import (
    ArchiveConnection,
    HistoryUnavailable,
    unavailable,
)
from pyaerial.store.archive.schema import (
    FLIGHT_STATUS_COMPLETED,
    HistorySchemaMismatch,
    drop_tables,
    ensure_schema,
)
from pyaerial.store.codec import (
    build_telemetry_docs,
    info_json,
    info_text_fields,
    integrity_rows,
)

log = logging.getLogger("pyaerial.store")

# Rows deleted per prune_frames call, so one prune never stalls the engine tick.
# The engine prunes again on the next tick while a call returns a full batch.
FRAME_PRUNE_BATCH = 20_000


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


def _alert_row(
    alert: dict[str, Any], flight_id: str, icao: str, callsign: str
) -> tuple:
    """``alerts`` columns in table order, starting alert_id, flight_id."""
    alert_id = (
        alert.get("alert_id")
        or f"{flight_id}:{alert.get('zone', '')}:{alert.get('rule', '')}"
    )
    lat, lon = _alert_lat_lon(alert)
    return (
        alert_id,
        flight_id,
        alert.get("icao", icao),
        alert.get("callsign") or callsign,
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


def _alert_lat_lon(alert: dict[str, Any]) -> tuple[Any, Any]:
    if alert.get("latitude") is not None or alert.get("longitude") is not None:
        return alert.get("latitude"), alert.get("longitude")
    position = alert.get("position") or {}
    coords = position.get("coordinates") or [None, None]
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return coords[1], coords[0]
    return None, None


class ArchiveWriter(ArchiveConnection):
    """The read-write archive: the engine's, and ``pyaerial reset``'s.

    Opening creates the file and its tables, or upgrades an older schema in
    place (see :mod:`pyaerial.store.archive.schema`). ``disabled`` (an
    isolated engine) writes nothing and reports itself safe to drop from.
    """

    def __init__(self, path: str | Path, *, disabled: bool = False):
        self._dropping_frames = False
        super().__init__(path, disabled=disabled)

    def _connect(self, *, recreate: bool = False) -> None:
        try:
            db_path = Path(self.path)
            if db_path.parent:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                if recreate:
                    drop_tables(conn)
                # Schema first: a newer or foreign file is refused before
                # anything (even its journal mode) is changed.
                ensure_schema(conn, self.path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
            except BaseException:
                conn.close()
                raise
            self._opened(conn)
        except HistorySchemaMismatch as exc:
            self._report("incompatible", str(exc))
        except (sqlite3.Error, OSError) as exc:
            self._report(
                "down",
                f"History database unavailable at {self.path} ({exc}); flights "
                "are kept for retry until it is writable.",
            )
            self._conn = None

    def health(self) -> dict[str, Any]:
        """``{"state": ok|down|disabled, "error": str|None}`` without I/O.

        ``down`` covers a missing connection and a connection whose last
        write failed (disk full, I/O error) until a write succeeds again.
        """
        if self.disabled:
            return {"state": "disabled", "error": None}
        ok = self._conn is not None and self._last_error is None
        return {"state": "ok" if ok else "down", "error": self._last_error}

    def _ensure_connected(self) -> bool:
        if self.disabled:
            return False
        with self._lock:
            self._reconnect_if_due()
            return self._conn is not None

    def finalize_plane(
        self,
        plane: Plane,
        *,
        alerts: list[dict[str, Any]] | None = None,
        retained: bool = True,
    ) -> bool:
        """Write a completed flight, or record that it is not kept.

        The caller decides whether the archive keeps the flight (the archive
        policy lives with the engine); this only writes what it is told.
        Returns True when it is safe to drop the live Redis copy: the flight
        was written, was intentionally discarded, or persistence is disabled.
        Returns False when the flight should have been written but SQLite was
        unavailable.
        """
        if self.disabled:
            return True
        alert_docs = alerts or []
        flight_id = plane.flight_id
        icao = plane.icao
        if not retained:
            log.debug("Discarded flight %s (archive policy)", flight_id)
            if not alert_docs:
                return True
            # Episodes carried over from an earlier, archived segment still
            # get their final state, on the row that segment wrote.
            return self._update_archived_alerts(alert_docs, flight_id, icao, plane)
        if not self._ensure_connected():
            return False
        telemetry_docs = build_telemetry_docs(plane, flight_id, icao)
        if self._persist_completed_flight(
            plane, flight_id, alert_docs, telemetry_docs=telemetry_docs
        ):
            log.debug("Retained completed flight %s", flight_id)
            return True
        return False

    def _persist_completed_flight(
        self,
        plane: Plane,
        flight_id: str,
        alerts: list[dict[str, Any]],
        *,
        telemetry_docs: list[dict[str, Any]] | None = None,
    ) -> bool:
        info = plane.info
        icao = plane.icao
        if telemetry_docs is None:
            telemetry_docs = build_telemetry_docs(plane, flight_id, icao)
        try:
            with self._session("persist") as conn:
                with conn:
                    # Decided under the write lock, from the archive itself:
                    # a failed check must mean "retry", never "not archived".
                    conn.execute("BEGIN IMMEDIATE")
                    if not self._should_write(conn, flight_id, telemetry_docs):
                        self._last_error = None
                        return True
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
                            FLIGHT_STATUS_COMPLETED,
                            plane.track.first_packet,
                            plane.track.last_update,
                            *info_text_fields(info).values(),
                            info_json(info),
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
                            integrity_score, integrity_status, integrity_flags,
                            gnss_height
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                doc.get("gnss_height"),
                            )
                            for doc in telemetry_docs
                        ],
                    )
                    conn.execute(
                        "DELETE FROM integrity WHERE flight_id = ?", (flight_id,)
                    )
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO integrity (
                            flight_id, timestamp, icao, score, status, flags, checks
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        integrity_rows(plane, flight_id, icao),
                    )
                    conn.execute("DELETE FROM alerts WHERE flight_id = ?", (flight_id,))
                    alert_rows = [
                        _alert_row(alert, flight_id, icao, info.callsign or "")
                        for alert in alerts
                    ]
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
            self._last_error = None
            return True
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.error("Failed to persist completed flight %s: %s", flight_id, exc)
            self._last_error = f"archive write failed: {exc}"
            return False

    def _update_archived_alerts(
        self, alerts: list[dict[str, Any]], flight_id: str, icao: str, plane: Plane
    ) -> bool:
        """Update the rows of episodes already archived; insert nothing.

        For a flight the policy discards: an episode carried over from an
        earlier segment already has a row there (kept on that segment), while
        an episode that began and ended in this flight is discarded with it.
        False (retry) when SQLite fails.
        """
        callsign = plane.info.callsign or ""
        rows = [_alert_row(alert, flight_id, icao, callsign) for alert in alerts]
        try:
            with self._session("alerts") as conn, conn:
                conn.executemany(
                    """
                    UPDATE alerts SET icao = ?, callsign = ?, zone = ?, rule = ?,
                        active = ?, activated_at = ?, deactivated_at = ?, eta = ?,
                        reason = ?, last_updated = ?, latitude = ?, longitude = ?,
                        altitude = ?
                    WHERE alert_id = ?
                    """,
                    [(*row[2:], row[0]) for row in rows],
                )
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.error("Failed to update archived alerts of %s: %s", flight_id, exc)
            self._last_error = f"archive write failed: {exc}"
            return False
        self._last_error = None
        return True

    @staticmethod
    def _should_write(
        conn: sqlite3.Connection, flight_id: str, telemetry_docs: list[dict[str, Any]]
    ) -> bool:
        """Whether to (re)write a flight; caller holds the write transaction.

        A completed flight is replaced only by a series covering a longer
        time span. Point counts do not decide it: a crash-recovery snapshot is
        clipped to the live window and can hold more points over less time.
        """
        row = conn.execute(
            "SELECT status FROM flights WHERE flight_id = ?", (flight_id,)
        ).fetchone()
        if row is None or row["status"] != FLIGHT_STATUS_COMPLETED:
            return True
        lo, hi = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM telemetry WHERE flight_id = ?",
            (flight_id,),
        ).fetchone()
        old_span = hi - lo if lo is not None else -1.0
        stamps = [doc["timestamp"] for doc in telemetry_docs]
        new_span = max(stamps) - min(stamps) if stamps else -1.0
        if new_span <= old_span:
            log.debug("Already archived completed flight %s", flight_id)
            return False
        log.info(
            "Upgrading archived flight %s (%.0f s -> %.0f s of telemetry)",
            flight_id,
            max(old_span, 0.0),
            new_span,
        )
        return True

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
            self._last_error = f"raw frame write failed: {exc}"
            return False
        if self._dropping_frames:
            log.info("Raw frame archiving resumed")
            self._dropping_frames = False
        self._last_error = None
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
                    (before, FRAME_PRUNE_BATCH),
                )
            return cursor.rowcount
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.warning("Could not prune raw frames: %s", exc)
            return 0

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
                drop_tables(conn)
                ensure_schema(conn, self.path)
        except sqlite3.Error as exc:
            raise unavailable("reset", exc) from exc
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
            raise unavailable("delete", exc) from exc
        return True
