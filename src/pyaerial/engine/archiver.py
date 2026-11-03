"""Flights into the archive: checkpoints of live flights, finalize, deferred
retries, recovery of flights a previous engine left behind, and segmentation
of long tracks.

A flight is written to the archive before its live copy is popped, so a
SQLite outage cannot lose it. Tick thread only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Protocol

from shapely import Polygon

from pyaerial.alerts.engine import ENDED_RESTART
from pyaerial.alerts.retain import should_retain
from pyaerial.config.schema import Config
from pyaerial.constants import STORE_LAT, STORE_LONG
from pyaerial.engine import segments
from pyaerial.engine.guard import StageGuard
from pyaerial.models import Plane
from pyaerial.store.archive.writer import FlightCheckpoint
from pyaerial.store.codec import (
    build_telemetry_docs,
    integrity_rows,
    plane_from_alert_docs,
)
from pyaerial.store.live import LiveUnavailable, LiveWriteStore

log = logging.getLogger("pyaerial.engine")

# Memory held by flights waiting for the archive (history down). Tracker-built
# planes keep the whole flight, measured at ~770 B per position sample (about
# 100 B per stored value), so a 30 min flight is ~2.8 MB. A count cap is
# neither a memory bound nor a loss bound: 256 long flights is gigabytes, 256
# short ones a few MB. pending_cost runs ~30% above measured, so real use
# stays under the budget; 1 GiB holds ~330 30 min flights (~160 flight-hours,
# ~2000 10 min ones). Beyond it the oldest fall back to their live Redis copy.
# Spilling to disk was rejected: the likeliest cause of a long outage is a
# full disk (raw_frames writes GBs a day to the same volume), where a spool
# fails too, and it would add a second recovery path.
PENDING_FINALIZE_BUDGET_BYTES = 1 << 30
_PENDING_BYTES_PER_VALUE = 100
_PENDING_BYTES_PER_PLANE = 4_096
# After an archive write fails, wait this long before retrying deferred flights.
_HISTORY_RETRY_INTERVAL = 10.0
# Per-tick time for archiving deferred and orphaned flights once history is
# back: ~25 ms per 30 min flight measured, so ~8 flights a tick. Catching up
# on hundreds in one tick took ~8 s, close to the 10 s heartbeat TTL.
# Shutdown's final attempt is not capped.
ARCHIVE_TICK_BUDGET = 0.2
# Refresh the heartbeat this often while archiving leftovers at startup.
STARTUP_HEARTBEAT_INTERVAL = 1.0
# Live flights are checkpointed into the archive this often: a crash loses
# at most CHECKPOINT_SECONDS + SETTLE_SECONDS of each flight's samples.
CHECKPOINT_SECONDS = 5.0
# A sample is checkpointed once it is this old: the tracker still rewrites
# the newest samples (a constant run keeps only its first and last datum,
# nearest-neighbour fields change when a later datum arrives). Verdicts are
# stamped when made and never change, so they need no settling. Finalize
# rewrites the whole flight, so a completed flight never depends on it.
SETTLE_SECONDS = 10.0


def pending_cost(plane: Plane) -> int:
    """Approximate bytes held by an in-memory plane (O(number of series))."""
    values = 0
    for bucket in (plane.received_data, plane.calculated_data):
        for series in bucket.values():
            values += len(series)
    values += len(plane.track.position_receivers)
    # A verdict carries the full check results: count it as ~10 values.
    values += 10 * len(plane.track.integrity_log)
    return _PENDING_BYTES_PER_PLANE + values * _PENDING_BYTES_PER_VALUE


def _position_samples(plane: Plane) -> int:
    """Telemetry samples the archive would write for *plane*.

    Equals ``len(build_telemetry_docs(plane, ...))`` without building them:
    one sample per latitude or longitude timestamp, when both series exist
    (see :func:`pyaerial.models.iter_telemetry_samples`).
    """
    recv = plane.received_data
    lat, lon = recv.get(STORE_LAT) or [], recv.get(STORE_LONG) or []
    if not lat or not lon:
        return 0
    return len({datum.time for datum in lat} | {datum.time for datum in lon})


def _closed_alert(alert: dict) -> dict:
    """Mark active episode closed with a deactivation timestamp."""
    doc = dict(alert)
    if doc.get("active") and not doc.get("deactivated_at"):
        doc["active"] = False
        doc["deactivated_at"] = time.time()
    return doc


class FlightArchive(Protocol):
    """The flight side of :class:`~pyaerial.store.ArchiveWriter`."""

    disabled: bool

    def ping(self) -> bool: ...

    def health(self) -> dict[str, Any]: ...

    def finalize_plane(
        self,
        plane: Plane,
        *,
        alerts: list[dict[str, Any]] | None = None,
        retained: bool = True,
    ) -> bool: ...

    def checkpoint(
        self,
        flights: Iterable[FlightCheckpoint],
        episodes: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]] = (),
        *,
        now: float,
    ) -> bool: ...


class _EpisodeCloser(Protocol):
    def deactivate(self, plane: Plane, ended_by: str | None = None) -> bool: ...


class AlertHooks(Protocol):
    """What the archiver needs from the pipeline (``PlaneCalculator``)."""

    @property
    def alerts(self) -> _EpisodeCloser: ...

    def deactivate_plane(self, plane: Plane) -> None: ...

    def handover(self, plane: Plane, old_flight_id: str, new_flight_id: str) -> None: ...


class FlightArchiver:
    """Archives finished flights and owns everything that can still lose one.

    - :meth:`finalize`: close alerts, archive, then pop the live copy; a
      failed write keeps the flight in :attr:`pending` (bounded by
      ``PENDING_FINALIZE_BUDGET_BYTES``; overflow counted as
      ``pending_evicted``) for retry with back-off.
    - :meth:`tick`: retries pending flights and sweeps orphaned live flights
      within a per-tick time budget.
    - :meth:`recover_on_startup`: leftovers of a previous engine process.
    - :meth:`split_long_flights`: segments of ``tracking.max_flight_seconds``.

    *tracked* returns the planes being tracked (``tracker.planes``): never
    swept as orphans. Test hooks: :attr:`pending`, :meth:`defer`,
    :meth:`clear_pending`, :meth:`retry_now`, :meth:`back_off`,
    ``max_flight_seconds`` and :func:`segments.start_new_segment`.
    """

    def __init__(
        self,
        config: Config,
        polygons: dict[str, Polygon],
        guard: StageGuard,
        *,
        archive: FlightArchive,
        live: LiveWriteStore,
        alerts: AlertHooks,
        tracked: Callable[[], Mapping[str, Plane]],
    ):
        self.config = config
        self.polygons = polygons
        self._guard = guard
        self._archive = archive
        self._live = live
        self._alerts = alerts
        self._tracked = tracked
        # Insertion-ordered: the first key is the oldest deferred flight.
        self._pending: dict[str, Plane] = {}
        # Evictions since history was last writable (logged once each way).
        self._pending_evicted = 0
        self._pending_evicted_total = 0
        self._history_retry_at = 0.0
        self._next_checkpoint = 0.0
        self.max_flight_seconds = float(
            getattr(
                config.tracking,
                "max_flight_seconds",
                segments.DEFAULT_MAX_FLIGHT_SECONDS,
            )
        )

    # -- state and health ----------------------------------------------------

    @property
    def pending(self) -> Mapping[str, Plane]:
        """Flights waiting for the archive, oldest first (read-only view)."""
        return MappingProxyType(self._pending)

    def health(self) -> dict[str, Any]:
        """``history``, ``history_error`` and ``history_pending`` for the
        ``live:engine`` heartbeat (compose healthcheck)."""
        health = self._archive.health()
        return {
            "history": health["state"],
            "history_error": health["error"],
            "history_pending": len(self._pending),
        }

    def counters(self) -> dict[str, Any]:
        """Cumulative since start: ``pending_evicted``."""
        return {"pending_evicted": self._pending_evicted_total}

    # -- finalize ------------------------------------------------------------

    def finalize(self, plane: Plane, *, from_snapshot: bool = False) -> bool:
        """Deactivate alerts, persist to SQLite, then drop the live Redis copy.

        Redis is only popped after the archive accepts the write (or the flight
        is intentionally not retained) so a disk outage cannot lose history.
        Returns False when the flight was deferred for retry.
        """
        # A broken alerter must not keep the flight out of the archive.
        with self._guard("alert deactivation"):
            self._alerts.deactivate_plane(plane)
        return self._archive_and_pop(plane, from_snapshot=from_snapshot)

    def _archive_and_pop(self, plane: Plane, *, from_snapshot: bool) -> bool:
        """Persist a flight whose alerts are closed, then pop its live copy."""
        flight_id = plane.flight_id
        if self._persist(flight_id, plane, from_snapshot=from_snapshot):
            self._pending.pop(flight_id, None)
            self._live.pop_flight(flight_id)
            return True
        log.warning(
            "Deferred persist for %s; keeping live data until history is writable",
            flight_id,
        )
        self.defer(flight_id, plane)
        return False

    def _persist(self, flight_id: str, plane: Plane, *, from_snapshot: bool) -> bool:
        """Archive one flight with its closed alerts. False keeps it for retry.

        An unexpected exception (a bug, not an outage) also returns False: the
        full in-memory plane and its live copy are kept instead of being lost
        with the engine.
        """
        try:
            alerts = [
                _closed_alert(alert)
                for alert in self._live.get_alerts(
                    flight_id=flight_id, active_only=False
                )
            ]
            store = self._archive
            # A disabled archive writes nothing: skip the policy work.
            retained = getattr(store, "disabled", False) or self.retain(
                plane, alerts, from_snapshot=from_snapshot
            )
            return store.finalize_plane(plane, alerts=alerts, retained=retained)
        except LiveUnavailable as exc:
            # The flight's alert episodes cannot be read: archive it later
            # rather than without them.
            log.warning("Deferring archive of %s: %s", flight_id, exc)
            return False
        except Exception:
            self._guard.failed("archive")
            return False

    # -- checkpoints ---------------------------------------------------------

    def checkpoint(self, now: float | None = None, *, force: bool = False) -> bool:
        """Write tracked flights' settled samples and new verdicts (one
        transaction), at most every CHECKPOINT_SECONDS unless *force*.

        Each flight with a position gets a ``live`` row; the cursors on its
        :class:`~pyaerial.models.TrackState` advance only once the write
        commits. Returns whether a checkpoint was written.
        """
        archive = self._archive
        if archive.disabled:
            return False
        mono = time.monotonic()
        if not force and mono < self._next_checkpoint:
            return False
        if not force and not self._history_retry_due():
            return False
        self._next_checkpoint = mono + CHECKPOINT_SECONDS
        now = time.time() if now is None else now
        until = now - SETTLE_SECONDS
        items: list[FlightCheckpoint] = []
        for plane in self._tracked().values():
            if not _position_samples(plane):
                continue
            track = plane.track
            flight_id, icao = plane.flight_id, plane.icao
            items.append(
                FlightCheckpoint(
                    plane,
                    build_telemetry_docs(
                        plane, flight_id, icao, since=track.archived_through, until=until
                    ),
                    integrity_rows(plane, flight_id, icao, after=track.verdicts_through),
                )
            )
        if not items:
            return False
        if not archive.checkpoint(items, now=now):
            self.back_off()
            return False
        for item in items:
            track = item.plane.track
            track.checkpoint_at = now
            if item.telemetry:
                track.archived_through = item.telemetry[-1]["timestamp"]
            if item.integrity:
                track.verdicts_through = item.integrity[-1][1]
        return True

    def retain(self, plane: Plane, alerts: list[dict], *, from_snapshot: bool) -> bool:
        """Whether the archive keeps this flight (``archive.flights`` policy).

        ``from_snapshot`` marks a crash/orphan reconstruction from the clipped
        live window: kept whenever it has a position or alerts, and a later
        longer series may upgrade the archive.
        """
        retained = should_retain(
            plane,
            alerts,
            self.config,
            self.polygons,
            positions=_position_samples(plane),
        )
        if not retained and from_snapshot:
            recv = plane.received_data
            retained = bool(alerts) or bool(recv.get(STORE_LAT))
        return retained

    # -- deferred flights ----------------------------------------------------

    def defer(self, flight_id: str, plane: Plane) -> None:
        """Keep a flight for archive retry within PENDING_FINALIZE_BUDGET_BYTES.

        Over the budget the oldest in-memory planes are dropped. Their live
        copy is not popped, so the orphan sweep still archives them (from the
        live snapshot, clipped to telemetry_keep_seconds) once history is
        writable again.
        """
        pending = self._pending
        pending.pop(flight_id, None)
        pending[flight_id] = plane
        budget = PENDING_FINALIZE_BUDGET_BYTES
        # Recomputed rather than tracked: enqueues only happen while history
        # is failing, and each cost is O(number of series).
        total = sum(pending_cost(p) for p in pending.values())
        while total > budget and len(pending) > 1:
            oldest = next(iter(pending))
            total -= pending_cost(pending.pop(oldest))
            if self._pending_evicted == 0:
                log.error(
                    "Deferred archive memory budget (%d MiB) reached; dropping "
                    "the oldest in-memory flights (live copies, clipped to "
                    "tracking.telemetry_keep_seconds, are kept for later archive)",
                    budget >> 20,
                )
            self._pending_evicted += 1
            self._pending_evicted_total += 1

    def clear_pending(self) -> None:
        """Forget every deferred flight (their live copies stay; tests)."""
        self._pending.clear()

    def _history_writable(self) -> bool:
        store = self._archive
        return store.disabled or store.ping()

    def _history_retry_due(self) -> bool:
        return time.monotonic() >= self._history_retry_at

    def back_off(self) -> None:
        """Pause retries and orphan sweeps for _HISTORY_RETRY_INTERVAL."""
        self._history_retry_at = time.monotonic() + _HISTORY_RETRY_INTERVAL

    def retry_now(self) -> None:
        """End a back-off: the next retry or sweep runs at once."""
        self._history_retry_at = 0.0

    def tick(self, budget: float | None = ARCHIVE_TICK_BUDGET) -> None:
        """Retry deferred flights, then sweep orphans, within *budget* seconds
        (``None``: no limit). Each step runs under its own stage guard."""
        deadline = None if budget is None else time.monotonic() + budget
        with self._guard("pending archive"):
            self.retry_pending(deadline)
        with self._guard("orphan archive"):
            self.sweep_orphans(deadline)
        with self._guard("checkpoint"):
            self.checkpoint()

    def retry_pending(self, deadline: float | None = None) -> None:
        """Archive deferred flights; with *deadline*, stop when it passes."""
        pending = self._pending
        if not pending or not self._history_retry_due():
            return
        if not self._history_writable():
            return
        tracked = {plane.flight_id for plane in self._tracked().values()}
        for index, (flight_id, plane) in enumerate(list(pending.items())):
            # At least one per tick, so a slow archive still makes progress.
            if index and deadline is not None and time.monotonic() >= deadline:
                return
            snapshot = flight_id not in tracked
            if not self._persist(flight_id, plane, from_snapshot=snapshot):
                # Disk full or I/O errors fail every write alike (while ping
                # still works): back off instead of retrying, and logging, every
                # pending flight each tick. The failed flight goes to the back
                # so one that keeps failing cannot block the rest.
                del pending[flight_id]
                pending[flight_id] = plane
                self.back_off()
                return
            del pending[flight_id]
            self._live.pop_flight(flight_id)
        if self._pending_evicted:
            log.warning(
                "History writable again; %d deferred flight(s) were dropped from "
                "memory while it was down and will be archived from live copies",
                self._pending_evicted,
            )
            self._pending_evicted = 0

    def sweep_orphans(self, deadline: float | None = None) -> None:
        """Archive leftover live flights; with *deadline*, stop when it passes."""
        if not self._history_retry_due():
            return
        if deadline is not None and time.monotonic() >= deadline:
            return
        tracked = {plane.flight_id for plane in self._tracked().values()}
        tracked |= set(self._pending)
        leftover = self._live.flight_ids() - tracked
        if leftover and not self._history_writable():
            return
        for flight_id in leftover:
            if deadline is not None and time.monotonic() >= deadline:
                return
            try:
                plane = self._live.plane_for_finalize(flight_id)
            except Exception:
                self._guard.failed("orphan snapshot")
                continue
            if plane is not None and not self.finalize(plane, from_snapshot=True):
                self.back_off()
                return

    def shutdown(self) -> None:
        """Last attempt, regardless of back-off and without a time budget: a
        flight still pending now is only recoverable from its clipped live
        copy at the next start."""
        self.retry_now()
        self.tick(None)

    # -- startup recovery ----------------------------------------------------

    def recover_on_startup(self) -> None:
        """Archive a previous engine's live flights, then close its stray alerts."""
        self._archive_startup_live_flights()
        self._close_stray_active_alerts()

    def _archive_startup_live_flights(self) -> None:
        """Persist leftover Redis live copies from a previous engine process.

        Whatever fails here stays in Redis for the main loop's orphan sweep.
        """
        with self._guard("startup archive"):
            beat = time.monotonic()
            for plane in self._live.planes_for_finalize():
                with self._guard("finalize"):
                    self.finalize(plane, from_snapshot=True)
                beat = self._keep_heartbeat(beat)

    def _keep_heartbeat(self, last: float) -> float:
        """Refresh the heartbeat during long startup work (10 s TTL)."""
        now = time.monotonic()
        if now - last < STARTUP_HEARTBEAT_INTERVAL:
            return last
        with self._guard("heartbeat"):
            self._live.touch_engine()
        return now

    def _close_stray_active_alerts(self) -> None:
        """Close active alerts whose flight never reached ``live:flights``.

        A crash between an alert write and that tick's flight write leaves an
        active alert neither the startup archive nor the orphan sweep can see.
        Each is deactivated like any restart leftover (the notification goes
        out with ``ended_by: restart``), and the closed episode is archived
        with a flight row that has no telemetry: the position is used for the
        notification only, never written as a sample.
        """
        with self._guard("stray alerts"):
            by_flight: dict[str, list[dict]] = {}
            for doc in self._live.stray_active_alerts():
                by_flight.setdefault(doc["flight_id"], []).append(doc)
            beat = time.monotonic()
            for flight_id, docs in by_flight.items():
                beat = self._keep_heartbeat(beat)
                plane = plane_from_alert_docs(flight_id, docs)
                if plane is None:
                    log.warning("Cannot close stray alerts of %s", flight_id)
                    continue
                log.warning(
                    "Closing %d alert(s) of %s left active by a crash before "
                    "the flight was written",
                    len(docs),
                    flight_id,
                )
                with self._guard("stray alert"):
                    self._alerts.alerts.deactivate(plane, ended_by=ENDED_RESTART)
                    self._archive_and_pop(
                        replace(plane, received_data={}), from_snapshot=True
                    )

    # -- segmentation --------------------------------------------------------

    def split_long_flights(self) -> None:
        """Archive tracks longer than tracking.max_flight_seconds as segments."""
        if self.max_flight_seconds <= 0:
            return
        for plane in list(self._tracked().values()):
            self.split_if_needed(plane)

    def split_if_needed(self, plane: Plane) -> bool:
        """Split *plane* when its track exceeds ``max_flight_seconds``."""
        limit = self.max_flight_seconds
        if limit <= 0:
            return False
        if plane.track.last_update - plane.track.first_packet <= limit:
            return False
        with self._guard("segment"):
            self._split_flight(plane)
        return True

    def _split_flight(self, plane: Plane) -> None:
        """Archive the track so far and keep tracking the aircraft as a new flight.

        The new flight starts at the last position sample, which both
        segments share, and keeps the last value of every series so speed,
        heading, ETA, and the verdict in effect carry on without a gap.
        Active alert episodes move to the new flight silently.
        """
        track = plane.track
        old_first = track.first_packet
        new_first, keep_position = segments.next_segment_start(
            plane, self.config.tracking.remember_planes
        )
        if int(new_first) <= int(old_first):
            return  # no distinct flight id possible yet
        if track.track_started is None:
            track.track_started = old_first
        old_flight_id = plane.flight_id
        # Snapshot first: from here on the segment is archived (or kept for
        # retry) whatever the later steps do, so a bug cannot lose the track.
        segment = segments.segment_copy(plane)
        new_flight_id: str | None = None
        try:
            segments.start_new_segment(plane, new_first, keep_position=keep_position)
            new_flight_id = plane.flight_id
        except Exception:
            self._guard.failed("segment trim")
        if new_flight_id is not None:
            carried = 0
            with self._guard("segment alerts"):
                carried = self._live.carry_active_alerts(old_flight_id, new_flight_id)
            with self._guard("segment alerts"):
                # Switch the alert engine to the new flight now, so an orphan
                # sweep of the old segment can never send a false deactivate.
                self._alerts.handover(plane, old_flight_id, new_flight_id)
            with self._guard("live store"):
                # Live before the old copy is popped: no tick without it.
                self._live.write_live_planes({new_flight_id: plane})
            log.info(
                "Flight %s exceeded tracking.max_flight_seconds (%.0f s); archived "
                "it and continue as %s%s",
                old_flight_id,
                self.max_flight_seconds,
                new_flight_id,
                f" ({carried} active alert(s) carried over)" if carried else "",
            )
        self._archive_and_pop(segment, from_snapshot=False)
