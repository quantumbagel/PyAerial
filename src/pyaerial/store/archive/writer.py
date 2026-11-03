"""Archive writer for the engine and ``pyaerial reset``: creates and upgrades
the schema, writes finished flights and raw frames, prunes and deletes."""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import Plane
from pyaerial.receivers.frames import RawFrame, frame_fields
from pyaerial.store.archive._conn import (
    ArchiveConnection,
    HistoryUnavailable,
    unavailable,
)
from pyaerial.store.archive.rows import alert_from_row
from pyaerial.store.archive.schema import (
    FLIGHT_STATUS_COMPLETED,
    FLIGHT_STATUS_LIVE,
    HistorySchemaMismatch,
    drop_tables,
    ensure_schema,
)
from pyaerial.store.codec import (
    build_telemetry_docs,
    flight_row,
    info_json,
    info_text_fields,
    integrity_rows,
)

log = logging.getLogger("pyaerial.store")

# Rows deleted per prune_frames call, so one prune never stalls the engine tick.
# The engine prunes again on the next tick while a call returns a full batch.
FRAME_PRUNE_BATCH = 20_000

_TELEMETRY_INSERT = """
    INSERT OR REPLACE INTO telemetry (
        flight_id, timestamp, icao, latitude, longitude, altitude, speed,
        heading, receivers, integrity_score, integrity_status,
        integrity_flags, gnss_height
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INTEGRITY_INSERT = """
    INSERT OR REPLACE INTO integrity (
        flight_id, timestamp, icao, score, status, flags, checks
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
"""
# A live flight row: created by its first checkpoint (or alert episode),
# refreshed by later ones. Never turns a finalized flight back into a live one.
_FLIGHT_UPSERT_LIVE = f"""
    INSERT INTO flights (
        flight_id, icao, status, start_time, end_time, retained, callsign,
        model, owner, country, aircraft_type, registration, info,
        checkpoint_at, previous_flight_id
    ) VALUES (?, ?, '{FLIGHT_STATUS_LIVE}', ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(flight_id) DO UPDATE SET
        end_time = MAX(flights.end_time, excluded.end_time),
        callsign = excluded.callsign, model = excluded.model,
        owner = excluded.owner, country = excluded.country,
        aircraft_type = excluded.aircraft_type,
        registration = excluded.registration, info = excluded.info,
        checkpoint_at = COALESCE(excluded.checkpoint_at, flights.checkpoint_at)
    WHERE flights.status = '{FLIGHT_STATUS_LIVE}'
"""
# One row per episode (alert_id), upserted on activate, while active and on
# deactivate. A closed episode is never reopened by a stale refresh, keeps
# the segment where it began and its original activation, and last_updated
# never moves back (re-recording at a segment handover).
_ALERT_UPSERT = """
    INSERT INTO alerts (
        alert_id, flight_id, icao, callsign, zone, rule, active,
        activated_at, deactivated_at, eta, reason, last_updated, latitude,
        longitude, altitude, start_flight_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(alert_id) DO UPDATE SET
        flight_id = excluded.flight_id, icao = excluded.icao,
        callsign = excluded.callsign, zone = excluded.zone,
        rule = excluded.rule, active = excluded.active,
        activated_at = COALESCE(alerts.activated_at, excluded.activated_at),
        deactivated_at = excluded.deactivated_at, eta = excluded.eta,
        reason = excluded.reason,
        last_updated = MAX(
            COALESCE(alerts.last_updated, excluded.last_updated),
            COALESCE(excluded.last_updated, alerts.last_updated)
        ),
        latitude = excluded.latitude, longitude = excluded.longitude,
        altitude = excluded.altitude,
        start_flight_id = COALESCE(alerts.start_flight_id, excluded.start_flight_id)
    WHERE NOT (alerts.active = 0 AND excluded.active = 1)
"""


class FlightCheckpoint(NamedTuple):
    """One live flight's slice for :meth:`ArchiveWriter.checkpoint`: the new
    telemetry docs (:func:`~pyaerial.store.codec.build_telemetry_docs` with
    ``since``/``until``) and verdict rows (``integrity_rows`` with ``after``)."""

    plane: Plane
    telemetry: list[dict[str, Any]]
    integrity: list[tuple]


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


def _telemetry_row(doc: dict[str, Any]) -> tuple:
    return (
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
        json_dumps(doc["integrity_flags"]) if "integrity_flags" in doc else None,
        doc.get("gnss_height"),
    )


def _live_flight_params(row: Mapping[str, Any], checkpoint_at: float | None) -> tuple:
    """``_FLIGHT_UPSERT_LIVE`` parameters for a :func:`codec.flight_row`."""
    return (
        row["flight_id"],
        row["icao"],
        row["start_time"],
        row["end_time"],
        row.get("callsign") or "",
        row.get("model") or "",
        row.get("owner") or "",
        row.get("country") or "",
        row.get("aircraft_type") or "",
        row.get("registration") or "",
        row.get("info") or "{}",
        checkpoint_at,
        row.get("previous_flight_id"),
    )


def _alert_upsert_params(
    alert: Mapping[str, Any], flight_id: str, icao: str, callsign: str
) -> tuple:
    """``_ALERT_UPSERT`` parameters; a new row began on *flight_id* unless the
    document names its ``start_flight_id``."""
    row = _alert_row(dict(alert), flight_id, icao, callsign)
    return (*row, alert.get("start_flight_id") or flight_id)


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
        #: Schema version the file had when this process first opened it (0:
        #: created now; None: not opened yet). Upgrade notes key off it.
        self.schema_found: int | None = None
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
                found = ensure_schema(conn, self.path)
                if self.schema_found is None:
                    self.schema_found = found
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
                        INSERT INTO flights (
                            flight_id, icao, status, start_time, end_time,
                            retained, callsign, model, owner, country,
                            aircraft_type, registration, info,
                            previous_flight_id
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(flight_id) DO UPDATE SET
                            status = excluded.status,
                            start_time = excluded.start_time,
                            end_time = excluded.end_time, retained = 1,
                            callsign = excluded.callsign,
                            model = excluded.model, owner = excluded.owner,
                            country = excluded.country,
                            aircraft_type = excluded.aircraft_type,
                            registration = excluded.registration,
                            info = excluded.info,
                            previous_flight_id = excluded.previous_flight_id
                        """,
                        (
                            flight_id,
                            icao,
                            FLIGHT_STATUS_COMPLETED,
                            plane.track.first_packet,
                            plane.track.last_update,
                            *info_text_fields(info).values(),
                            info_json(info),
                            plane.track.previous_flight_id,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM telemetry WHERE flight_id = ?", (flight_id,)
                    )
                    conn.executemany(
                        _TELEMETRY_INSERT,
                        [_telemetry_row(doc) for doc in telemetry_docs],
                    )
                    conn.execute(
                        "DELETE FROM integrity WHERE flight_id = ?", (flight_id,)
                    )
                    conn.executemany(
                        _INTEGRITY_INSERT, integrity_rows(plane, flight_id, icao)
                    )
                    # By alert_id: a row keeps the segment its episode began
                    # on (start_flight_id) when a later segment updates it.
                    conn.executemany(
                        _ALERT_UPSERT,
                        [
                            _alert_upsert_params(
                                alert, flight_id, icao, info.callsign or ""
                            )
                            for alert in alerts
                        ],
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

    # -- incremental (live) writes ------------------------------------------

    def checkpoint(
        self,
        flights: Iterable[FlightCheckpoint],
        episodes: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]] = (),
        *,
        now: float,
    ) -> bool:
        """Write live flights' new samples and verdicts, and refresh episodes.

        One transaction: each flight's ``live`` row is created or refreshed
        (``checkpoint_at = now``), its telemetry and verdict slices inserted,
        and *episodes* (``(flight_row, alert_doc)`` pairs, see
        :meth:`upsert_episodes`) upserted. False (nothing written; the caller
        keeps its cursors) when the archive is unavailable.
        """
        if self.disabled:
            return True
        if not self._ensure_connected():
            return False
        try:
            with self._session("checkpoint") as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                for item in flights:
                    conn.execute(
                        _FLIGHT_UPSERT_LIVE,
                        _live_flight_params(flight_row(item.plane), now),
                    )
                    if item.telemetry:
                        conn.executemany(
                            _TELEMETRY_INSERT,
                            [_telemetry_row(doc) for doc in item.telemetry],
                        )
                    if item.integrity:
                        conn.executemany(_INTEGRITY_INSERT, item.integrity)
                self._write_episodes(conn, episodes)
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.error("Failed to checkpoint live flights: %s", exc)
            self._last_error = f"archive write failed: {exc}"
            return False
        self._last_error = None
        return True

    def upsert_episodes(
        self, episodes: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
    ) -> bool:
        """Record alert episodes, each with its flight, in one transaction.

        Each item is ``(flight_row, alert_doc)``: a
        :func:`~pyaerial.store.codec.flight_row` and the episode document.
        The flight's ``live`` row is written first (created if this is its
        first write), so an episode never exists without its flight. False
        when the archive is unavailable (the caller keeps them for retry).
        """
        if self.disabled or not episodes:
            return True
        if not self._ensure_connected():
            return False
        try:
            with self._session("alert episodes") as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                self._write_episodes(conn, episodes)
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.error("Failed to record alert episodes: %s", exc)
            self._last_error = f"archive write failed: {exc}"
            return False
        self._last_error = None
        return True

    @staticmethod
    def _write_episodes(
        conn: sqlite3.Connection,
        episodes: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    ) -> None:
        for flight, alert in episodes:
            conn.execute(_FLIGHT_UPSERT_LIVE, _live_flight_params(flight, None))
            conn.execute(
                _ALERT_UPSERT,
                _alert_upsert_params(
                    alert,
                    str(alert.get("flight_id") or flight["flight_id"]),
                    str(flight["icao"]),
                    str(flight.get("callsign") or ""),
                ),
            )

    def flight_alerts(self, flight_id: str) -> list[dict[str, Any]]:
        """Episodes whose row points at *flight_id* (ended there, or still
        open on it). Raises :class:`HistoryUnavailable`: the caller must not
        decide retention without them."""
        try:
            with self._session("flight alerts") as conn:
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE flight_id = ? ORDER BY activated_at",
                    (flight_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            self._last_error = f"archive read failed: {exc}"
            raise unavailable("flight alerts", exc) from exc
        return [alert_from_row(row) for row in rows]

    def discard_flight(self, flight_id: str) -> bool:
        """Delete a flight the archive policy does not keep, in one transaction.

        Its flight, telemetry and verdict rows go, with the closed episodes
        that began on it. A closed episode carried over from an earlier
        segment goes back to that segment's row if it was kept (else it goes
        too), so every alert row points at an existing flight. Open episodes
        are left to the caller (a segment split carries them on). False when
        the archive is unavailable.
        """
        if self.disabled:
            return True
        if not self._ensure_connected():
            return False
        try:
            with self._session("discard") as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                for table in ("telemetry", "integrity", "flights"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE flight_id = ?", (flight_id,)
                    )
                conn.execute(
                    """
                    UPDATE alerts SET flight_id = start_flight_id
                    WHERE flight_id = ? AND active = 0
                        AND start_flight_id IS NOT NULL
                        AND start_flight_id != flight_id
                        AND start_flight_id IN (SELECT flight_id FROM flights)
                    """,
                    (flight_id,),
                )
                conn.execute(
                    "DELETE FROM alerts WHERE flight_id = ? AND active = 0",
                    (flight_id,),
                )
        except (sqlite3.Error, HistoryUnavailable) as exc:
            log.error("Failed to discard flight %s: %s", flight_id, exc)
            self._last_error = f"archive write failed: {exc}"
            return False
        self._last_error = None
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
