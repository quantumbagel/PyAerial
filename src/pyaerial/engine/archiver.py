"""Flights into the archive: checkpoints of live flights, finalize, deferred
retries, recovery of what a previous engine left open, and segmentation of
long tracks.

The archive is the record of every flight and alert episode: a tracked
flight is checkpointed into it while live, and finalized (rewritten whole,
or deleted by the archive policy) when it ends. Tick thread only.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

from shapely import Polygon

from pyaerial.alerts.retain import should_retain
from pyaerial.config.schema import Config
from pyaerial.constants import STORE_LAT, STORE_LONG
from pyaerial.engine import segments
from pyaerial.engine.guard import StageGuard
from pyaerial.models import AircraftInfo, Plane, TrackState
from pyaerial.store.archive import HistoryUnavailable
from pyaerial.store.archive.schema import LIFECYCLE_SCHEMA_VERSION
from pyaerial.store.archive.writer import FlightCheckpoint
from pyaerial.store.codec import build_telemetry_docs, integrity_rows, plane_from_rows

log = logging.getLogger("pyaerial.engine")

# Memory held by flights waiting for the archive (history down). Tracker-built
# planes keep the whole flight, measured at ~770 B per position sample (about
# 100 B per stored value), so a 30 min flight is ~2.8 MB. A count cap is
# neither a memory bound nor a loss bound: 256 long flights is gigabytes, 256
# short ones a few MB. pending_cost runs ~30% above measured, so real use
# stays under the budget; 1 GiB holds ~330 30 min flights (~160 flight-hours,
# ~2000 10 min ones). Beyond it the oldest are dropped, keeping only what
# their checkpoints wrote (closed as interrupted). Spilling to disk was
# rejected: the likeliest cause of a long outage is a full disk (raw_frames
# writes GBs a day to the same volume), where a spool fails too.
PENDING_FINALIZE_BUDGET_BYTES = 1 << 30
_PENDING_BYTES_PER_VALUE = 100
_PENDING_BYTES_PER_PLANE = 4_096
# After an archive write fails, wait this long before retrying deferred flights.
_HISTORY_RETRY_INTERVAL = 10.0
# Per-tick time for archiving deferred flights once history is back: ~25 ms
# per 30 min flight measured, so ~8 flights a tick. Catching up on hundreds
# in one tick took ~8 s, close to the 10 s heartbeat TTL.
# Shutdown's final attempt is not capped.
ARCHIVE_TICK_BUDGET = 0.2
# Refresh the heartbeat this often during startup recovery.
STARTUP_HEARTBEAT_INTERVAL = 1.0
# Each live flight is checkpointed into the archive this often, spread over
# the ticks: each tick writes the most overdue share of the flights, sized
# from the time since the last tick, so slow ticks write more per tick and
# every flight still goes out every CHECKPOINT_SECONDS. A crash loses at most
# CHECKPOINT_SECONDS + SETTLE_SECONDS plus one tick (~15 s) per flight.
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


def _has_position(plane: Plane) -> bool:
    recv = plane.received_data
    return bool(recv.get(STORE_LAT)) and bool(recv.get(STORE_LONG))


def _closed_alert(alert: dict) -> dict:
    """Mark an episode still open at finalize closed (its deactivation failed:
    no deactivate notification was sent for it)."""
    doc = dict(alert)
    if doc.get("active") and not doc.get("deactivated_at"):
        log.warning(
            "Closing alert %s of %s at finalize without a deactivate "
            "notification (its deactivation failed)",
            doc.get("alert_id"),
            doc.get("flight_id"),
        )
        doc["active"] = False
        doc["deactivated_at"] = time.time()
    return doc


def _flight_id_plane(flight_id: str, callsign: str) -> Plane | None:
    """A minimal plane for an episode whose flight row is not live (or gone):
    ICAO and first packet from the id (``{icao}-{first_packet}``)."""
    icao, _, first_text = flight_id.rpartition("-")
    try:
        first = float(int(first_text))
    except ValueError:
        return None
    if not icao:
        return None
    return Plane(
        info=AircraftInfo(icao=icao, callsign=callsign or None),
        track=TrackState(first_packet=first, last_update=first),
    )


class FlightArchive(Protocol):
    """The flight side of :class:`~pyaerial.store.archive.writer.ArchiveWriter`."""

    disabled: bool
    #: Schema version the file had when first opened (None: not opened yet).
    schema_found: int | None

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

    def flight_alerts(self, flight_id: str) -> list[dict[str, Any]]: ...

    def live_flights(self) -> list[dict[str, Any]]: ...

    def active_episodes(self) -> list[dict[str, Any]]: ...

    def flight_points(self, flight_id: str) -> list[dict[str, Any]]: ...

    def interrupt_flight(self, flight_id: str) -> bool: ...

    def discard_flight(self, flight_id: str) -> bool: ...


class LiveCache(Protocol):
    """What the archiver needs from the live store: the heartbeat during long
    startup work, and dropping a previous process's cache."""

    def touch_engine(self) -> None: ...

    def clear_cache(self) -> int: ...


class EpisodeLog(Protocol):
    """The archive side of :class:`~pyaerial.store.recorder.EpisodeRecorder`."""

    def checkpoint_episodes(
        self, flight_ids: Collection[str] | None = None
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]: ...

    def written(
        self, entries: Sequence[tuple[dict[str, Any], dict[str, Any]]]
    ) -> None: ...

    def drain(self, flight_id: str | None = None) -> bool: ...

    def restore_outbox(self) -> int: ...

    def open_docs(self, flight_id: str) -> list[dict[str, Any]]: ...


class AlertHooks(Protocol):
    """What the archiver needs from the pipeline (``PlaneCalculator``)."""

    def deactivate_plane(self, plane: Plane) -> None: ...

    def close_restored(self, plane: Plane, doc: Mapping[str, Any]) -> None: ...

    def handover(self, plane: Plane, old_flight_id: str, new_flight_id: str) -> None: ...


class FlightArchiver:
    """Archives flights and owns everything that can still lose one.

    - :meth:`checkpoint`: tracked flights' settled samples, new verdicts and
      open episodes, every CHECKPOINT_SECONDS.
    - :meth:`finalize`: close alerts, then archive the whole flight (or
      delete it, per ``archive.flights``); a failed write keeps the flight
      in :attr:`pending` (bounded by ``PENDING_FINALIZE_BUDGET_BYTES``;
      overflow counted as ``pending_evicted``) for retry with back-off.
    - :meth:`recover_on_startup`: closes what a previous engine process left
      open (its live flights and their episodes); retried by :meth:`tick`
      while the archive is down.
    - :meth:`split_long_flights`: segments of ``tracking.max_flight_seconds``.

    *tracked* returns the planes being tracked (``tracker.planes``). Test
    hooks: :attr:`pending`, :meth:`defer`, :meth:`clear_pending`,
    :meth:`retry_now`, :meth:`back_off`, ``max_flight_seconds`` and
    :func:`segments.start_new_segment`.
    """

    def __init__(
        self,
        config: Config,
        polygons: dict[str, Polygon],
        guard: StageGuard,
        *,
        archive: FlightArchive,
        live: LiveCache,
        alerts: AlertHooks,
        tracked: Callable[[], Mapping[str, Plane]],
        episodes: EpisodeLog | None = None,
    ):
        self.config = config
        self.polygons = polygons
        self._guard = guard
        self._archive = archive
        self._live = live
        self._alerts = alerts
        self._tracked = tracked
        self._episodes = episodes
        # Insertion-ordered: the first key is the oldest deferred flight.
        self._pending: dict[str, Plane] = {}
        # Open episodes a split carried on from a pending segment: they count
        # for its retention, though their rows moved to the next segment.
        self._carried: dict[str, list[dict[str, Any]]] = {}
        # Evictions since history was last writable (logged once each way).
        self._pending_evicted = 0
        self._pending_evicted_total = 0
        self._history_retry_at = 0.0
        # Wall time of the previous checkpoint call (slice sizing).
        self._last_checkpoint_call = time.time()
        # Whether the archive holds nothing left open by another process
        # (startup recovery done, and redone after evictions).
        self._recovered = True
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

    def finalize(self, plane: Plane) -> bool:
        """Deactivate alerts, then persist the flight to SQLite (or delete it,
        per the archive policy). Its live copy goes with the next publish.

        Returns False when the flight was deferred for retry.
        """
        # A broken alerter must not keep the flight out of the archive.
        with self._guard("alert deactivation"):
            self._alerts.deactivate_plane(plane)
        return self._archive_flight(plane)

    def _archive_flight(
        self, plane: Plane, carried: Sequence[dict[str, Any]] = ()
    ) -> bool:
        """Persist a flight whose alerts are closed, or defer it."""
        flight_id = plane.flight_id
        if carried:
            self._carried[flight_id] = list(carried)
        if self._persist(flight_id, plane):
            self._pending.pop(flight_id, None)
            self._carried.pop(flight_id, None)
            return True
        log.warning(
            "Deferred persist for %s; keeping it in memory until history is writable",
            flight_id,
        )
        self.defer(flight_id, plane)
        return False

    def _persist(self, flight_id: str, plane: Plane) -> bool:
        """Archive one flight with its closed alerts. False keeps it for retry.

        Its episodes are read from the archive (after any the archive refused
        earlier are re-sent), so retention never decides without them. An
        unexpected exception (a bug, not an outage) also returns False: the
        full in-memory plane is kept instead of being lost with the engine.
        """
        store = self._archive
        try:
            if store.disabled:
                # Writes nothing: skip the policy work.
                return store.finalize_plane(plane, alerts=[], retained=True)
            # Only this flight's unarchived episodes must be in first.
            if self._episodes is not None and not self._episodes.drain(flight_id):
                return False
            alerts = [_closed_alert(doc) for doc in store.flight_alerts(flight_id)]
            retained = self.retain(plane, [*alerts, *self._carried.get(flight_id, ())])
            return store.finalize_plane(plane, alerts=alerts, retained=retained)
        except HistoryUnavailable as exc:
            log.warning("Deferring archive of %s: %s", flight_id, exc)
            return False
        except Exception:
            self._guard.failed("archive")
            return False

    def retain(self, plane: Plane, alerts: list[dict]) -> bool:
        """Whether the archive keeps this flight (``archive.flights`` policy)."""
        return should_retain(
            plane,
            alerts,
            self.config,
            self.polygons,
            positions=_position_samples(plane),
        )

    # -- deferred flights ----------------------------------------------------

    def defer(self, flight_id: str, plane: Plane) -> None:
        """Keep a flight for archive retry within PENDING_FINALIZE_BUDGET_BYTES.

        Over the budget the oldest in-memory planes are dropped. What their
        checkpoints wrote stays in the archive as a live flight, closed as
        interrupted once history is writable again.
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
            self._carried.pop(oldest, None)
            if self._pending_evicted == 0:
                log.error(
                    "Deferred archive memory budget (%d MiB) reached; dropping "
                    "the oldest in-memory flights (their checkpointed part is "
                    "kept as interrupted flights)",
                    budget >> 20,
                )
            self._pending_evicted += 1
            self._pending_evicted_total += 1
            self._recovered = False

    def clear_pending(self) -> None:
        """Forget every deferred flight (tests)."""
        self._pending.clear()
        self._carried.clear()

    def _history_writable(self) -> bool:
        store = self._archive
        return store.disabled or store.ping()

    def _history_retry_due(self) -> bool:
        return time.monotonic() >= self._history_retry_at

    def back_off(self) -> None:
        """Pause retries, recovery and checkpoints for _HISTORY_RETRY_INTERVAL."""
        self._history_retry_at = time.monotonic() + _HISTORY_RETRY_INTERVAL

    def retry_now(self) -> None:
        """End a back-off: the next retry or checkpoint runs at once."""
        self._history_retry_at = 0.0

    def tick(self, budget: float | None = ARCHIVE_TICK_BUDGET) -> None:
        """Retry deferred flights within *budget* seconds (``None``: no
        limit), finish recovery if the archive was down, then checkpoint.
        Each step runs under its own stage guard."""
        deadline = None if budget is None else time.monotonic() + budget
        with self._guard("pending archive"):
            self.retry_pending(deadline)
        if not self._recovered:
            with self._guard("startup recovery"):
                self._recover()
        with self._guard("checkpoint"):
            self.checkpoint()

    def retry_pending(self, deadline: float | None = None) -> None:
        """Archive deferred flights; with *deadline*, stop when it passes."""
        pending = self._pending
        if not pending or not self._history_retry_due():
            return
        if not self._history_writable():
            return
        for index, (flight_id, plane) in enumerate(list(pending.items())):
            # At least one per tick, so a slow archive still makes progress.
            if index and deadline is not None and time.monotonic() >= deadline:
                return
            if not self._persist(flight_id, plane):
                # Disk full or I/O errors fail every write alike (while ping
                # still works): back off instead of retrying, and logging, every
                # pending flight each tick. The failed flight goes to the back
                # so one that keeps failing cannot block the rest.
                del pending[flight_id]
                pending[flight_id] = plane
                self.back_off()
                return
            del pending[flight_id]
            self._carried.pop(flight_id, None)
        if self._pending_evicted:
            log.warning(
                "History writable again; %d deferred flight(s) were dropped from "
                "memory while it was down and are archived as interrupted",
                self._pending_evicted,
            )
            self._pending_evicted = 0

    def shutdown(self) -> None:
        """Last attempt, regardless of back-off and without a time budget: a
        flight still pending now keeps only its checkpoints (interrupted at
        the next start)."""
        self.retry_now()
        self.tick(None)

    # -- checkpoints ---------------------------------------------------------

    def checkpoint(self, now: float | None = None, *, force: bool = False) -> bool:
        """Write the due flights' settled samples, new verdicts and open
        episodes (one transaction). Returns whether anything was written.

        A flight is due CHECKPOINT_SECONDS after its last checkpoint. Each
        call takes the most overdue ``elapsed / CHECKPOINT_SECONDS`` share of
        the tracked flights (*elapsed*: wall time since the previous call),
        so the work is spread over the ticks instead of one burst every
        CHECKPOINT_SECONDS (p95 ~90 ms at 300 aircraft), and slow ticks do
        not stretch the interval.
        *force* writes every tracked flight now. The cursors on each
        :class:`~pyaerial.models.TrackState` advance only once the write
        commits; a flight gets its ``live`` row with its first checkpoint.
        """
        archive = self._archive
        if archive.disabled:
            return False
        if not force and not self._history_retry_due():
            return False
        # Unarchived episodes first, in their own transactions: one the
        # archive rejects must never fail the checkpoint.
        if self._episodes is not None and not self._episodes.drain():
            self.back_off()
            return False
        now = time.time() if now is None else now
        until = now - SETTLE_SECONDS
        planes = [plane for plane in self._tracked().values() if _has_position(plane)]
        elapsed = min(max(now - self._last_checkpoint_call, 0.0), CHECKPOINT_SECONDS)
        self._last_checkpoint_call = now
        if not force:
            due = [
                plane
                for plane in planes
                if plane.track.checkpoint_at is None
                or not 0 <= now - plane.track.checkpoint_at < CHECKPOINT_SECONDS
            ]
            due.sort(key=lambda plane: plane.track.checkpoint_at or -math.inf)
            share = math.ceil(len(planes) * elapsed / CHECKPOINT_SECONDS)
            planes = due[: max(1, share)]
        items: list[FlightCheckpoint] = []
        for plane in planes:
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
        episodes = (
            self._episodes.checkpoint_episodes(
                None if force else {item.plane.flight_id for item in items}
            )
            if self._episodes
            else []
        )
        if not items and not episodes:
            return False
        if not archive.checkpoint(items, episodes, now=now):
            self.back_off()
            return False
        if self._episodes is not None:
            self._episodes.written(episodes)
        for item in items:
            track = item.plane.track
            track.checkpoint_at = now
            track.in_archive = True
            if item.telemetry:
                track.archived_through = item.telemetry[-1]["timestamp"]
            if item.integrity:
                track.verdicts_through = item.integrity[-1][1]
        return True

    # -- startup recovery ----------------------------------------------------

    def recover_on_startup(self) -> None:
        """Close what a previous engine process left open in the archive.

        Runs before tracking starts; if the archive is down it is retried by
        :meth:`tick` once it is writable.
        """
        self._recovered = self._archive.disabled
        if not self._recovered:
            if self._episodes is not None:
                with self._guard("episode outbox"):
                    self._episodes.restore_outbox()
            with self._guard("startup recovery"):
                self._recover()
        # Redis is only a cache of what this process tracks: drop whatever a
        # previous one left (each tick republishes the current state).
        stale = 0
        with self._guard("live cache"):
            stale = self._live.clear_cache()
        found = self._archive.schema_found
        if stale and found is not None and found < LIFECYCLE_SCHEMA_VERSION:
            log.warning(
                "Discarded %d live flight(s) a pre-1.0 PyAerial left in Redis "
                "(it was not stopped gracefully before the upgrade); they are "
                "not in the archive",
                stale,
            )

    def _recover(self) -> bool:
        """Close the archive's live flights and open episodes that are not
        this process's (tracked or pending).

        1. Each open episode gets its one deactivate (``ended_by: restart``)
           and is recorded closed at its last refresh.
        2. Each live flight is kept as ``interrupted`` (ending at its last
           checkpointed sample) or deleted, by the archive policy decided on
           what the archive holds. A flight whose episode could not be
           closed stays ``live`` for the next start, so the episode is
           neither lost nor left pointing at a deleted flight.

        Idempotent: a crash part way repeats only the unfinished steps (and
        at most the one deactivate whose record had not committed).
        """
        if not self._history_retry_due() or not self._history_writable():
            return False
        # Episodes a previous engine could not archive go in first.
        if self._episodes is not None and not self._episodes.drain():
            self.back_off()
            return False
        archive = self._archive
        owned = {plane.flight_id for plane in self._tracked().values()}
        owned |= set(self._pending)
        try:
            flights = {doc["flight_id"]: doc for doc in archive.live_flights()}
            episodes = [
                doc
                for doc in archive.active_episodes()
                if doc.get("flight_id") not in owned
            ]
            planes: dict[str, Plane] = {}
            unclosed: set[str] = set()
            closed = 0
            beat = time.monotonic()
            for doc in episodes:
                flight_id = str(doc.get("flight_id") or "")
                plane = planes.get(flight_id)
                if plane is None:
                    flight = flights.get(flight_id)
                    plane = (
                        plane_from_rows(flight, archive.flight_points(flight_id))
                        if flight is not None
                        else _flight_id_plane(flight_id, str(doc.get("callsign") or ""))
                    )
                if plane is None:
                    log.warning("Cannot close alert %s of %s", doc.get("alert_id"), flight_id)
                    unclosed.add(flight_id)
                    continue
                planes[flight_id] = plane
                try:
                    self._alerts.close_restored(plane, doc)
                except Exception:
                    self._guard.failed("restart alert")
                    unclosed.add(flight_id)
                else:
                    closed += 1
                beat = self._keep_heartbeat(beat)
            # Closing an episode of a flight with no row gave it one.
            leftovers = [
                doc
                for doc in archive.live_flights()
                if doc["flight_id"] not in owned and doc["flight_id"] not in unclosed
            ]
            kept = 0
            for flight in leftovers:
                flight_id = flight["flight_id"]
                plane = planes.get(flight_id) or plane_from_rows(
                    flight, archive.flight_points(flight_id)
                )
                if self.retain(plane, archive.flight_alerts(flight_id)):
                    done = archive.interrupt_flight(flight_id)
                    kept += done
                else:
                    done = archive.discard_flight(flight_id)
                if not done:
                    self.back_off()
                    return False
                beat = self._keep_heartbeat(beat)
        except HistoryUnavailable as exc:
            log.warning("Deferring recovery of interrupted flights: %s", exc)
            self.back_off()
            return False
        if leftovers or closed:
            log.warning(
                "Recovered %d flight(s) a previous engine left open (%d kept as "
                "interrupted) and closed %d alert(s) it left active",
                len(leftovers),
                kept,
                closed,
            )
        if unclosed:
            log.error(
                "Could not close the alerts of %d flight(s) a previous engine left "
                "open; they stay live until the next start: %s",
                len(unclosed),
                ", ".join(sorted(unclosed)),
            )
        self._recovered = True
        return True

    def _keep_heartbeat(self, last: float) -> float:
        """Refresh the heartbeat during long startup work (10 s TTL)."""
        now = time.monotonic()
        if now - last < STARTUP_HEARTBEAT_INTERVAL:
            return last
        with self._guard("heartbeat"):
            self._live.touch_engine()
        return now

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
        carried: list[dict[str, Any]] = []
        if self._episodes is not None:
            with self._guard("segment alerts"):
                carried = self._episodes.open_docs(old_flight_id)
        new_flight_id: str | None = None
        try:
            segments.start_new_segment(plane, new_first, keep_position=keep_position)
            new_flight_id = plane.flight_id
        except Exception:
            self._guard.failed("segment trim")
        if new_flight_id is not None:
            with self._guard("segment alerts"):
                # The alert engine and the episode rows move to the new flight.
                self._alerts.handover(plane, old_flight_id, new_flight_id)
            log.info(
                "Flight %s exceeded tracking.max_flight_seconds (%.0f s); archived "
                "it and continue as %s%s",
                old_flight_id,
                self.max_flight_seconds,
                new_flight_id,
                f" ({len(carried)} active alert(s) carried over)" if carried else "",
            )
        # The next publish shows the new flight and drops the old one at once.
        self._archive_flight(segment, carried)
