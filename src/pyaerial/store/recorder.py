"""Alert episodes into the archive (the record) and the live store (the cache).

:class:`EpisodeRecorder` is the tracking engine's
:class:`~pyaerial.store.live.EpisodeStore`: the alert engine records each
activation and deactivation through it, in the order it notifies.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Protocol

from pyaerial.models import Plane
from pyaerial.store.archive._conn import RowRejected
from pyaerial.store.codec import alert_episode_doc, flight_row

log = logging.getLogger("pyaerial.store")

# (flight_row, alert_doc): an episode as ArchiveWriter.upsert_episodes takes it.
EpisodeEntry = tuple[dict[str, Any], dict[str, Any]]
# Episode writes waiting for the archive while it is down, latest state per
# alert_id (~1 KB each). A busy site raises a few hundred alerts an hour, so
# this covers days of outage; beyond it the oldest are dropped (counted).
OUTBOX_MAX = 10_000
# An outbox entry the archive rejects this many times (its content, or a bug:
# anything but an outage) is dropped, logged at ERROR and counted in
# episodes_dropped, so one bad entry cannot hold up the others.
QUARANTINE_AFTER = 3

_Result = str  # "ok" | "down" (archive unavailable) | "rejected"


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text_or_none(row: Mapping[str, Any], *keys: str) -> bool:
    return all(row.get(key) is None or isinstance(row[key], str) for key in keys)


def valid_entry(entry: Any) -> EpisodeEntry | None:
    """*entry* as an :data:`EpisodeEntry` if it has every key the archive
    needs with the right type (an outbox entry read back from Redis), else
    None."""
    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
        return None
    flight, doc = entry
    if not isinstance(flight, dict) or not isinstance(doc, dict):
        return None
    flight_id = flight.get("flight_id")
    if not (
        isinstance(flight_id, str)
        and flight_id
        and isinstance(flight.get("icao"), str)
        and _number(flight.get("start_time"))
        and _number(flight.get("end_time"))
        and _text_or_none(
            flight,
            "callsign",
            "model",
            "owner",
            "country",
            "aircraft_type",
            "registration",
            "info",
            "previous_flight_id",
        )
    ):
        return None
    if not (
        isinstance(doc.get("alert_id"), str)
        and doc["alert_id"]
        and doc.get("flight_id") == flight_id
        and isinstance(doc.get("active"), bool)
        and _number(doc.get("activated_at"))
        and all(
            doc.get(key) is None or _number(doc[key])
            for key in ("deactivated_at", "last_updated", "eta", "altitude")
        )
        and _text_or_none(doc, "zone", "rule", "icao", "callsign", "start_flight_id")
    ):
        return None
    return flight, doc


class EpisodeArchive(Protocol):
    """The episode side of :class:`~pyaerial.store.ArchiveWriter`."""

    def upsert_episodes(
        self, episodes: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
    ) -> bool: ...


class EpisodeCache(Protocol):
    """The live store side: the portal's cache, and a copy of the outbox
    (``live:episode_outbox``) so a crash during an outage loses no episode."""

    def cache_episode(self, doc: Mapping[str, Any]) -> None: ...

    def save_outbox(self, entries: Mapping[str, Any]) -> None: ...

    def drop_outbox(self, alert_ids: Iterable[str]) -> None: ...

    def load_outbox(self) -> list[Any]: ...

    def replace_outbox(self, entries: Mapping[str, Any]) -> None: ...


class EpisodeRecorder:
    """Records alert episodes in the archive at once, then in the live cache.

    - Activation and deactivation: upserted into the archive with the
      flight's row (one small transaction), then written to the live store.
    - While-active refreshes (ETA, position, ``last_updated``): kept in
      memory per open episode, written to the archive with the next
      checkpoint (:meth:`checkpoint_episodes`), and to the live store now.
    - A write the archive refuses (outage) waits in the outbox, latest state
      per alert_id (at most OUTBOX_MAX), and is re-sent before every
      checkpoint, in its own transaction, until it commits. It is copied to
      Redis while it waits, so the next engine replays it after a crash
      (:meth:`restore_outbox`). An entry the archive rejects for its content
      is retried alone and dropped after QUARANTINE_AFTER rejections.

    Tick thread only.
    """

    def __init__(self, archive: EpisodeArchive, live: EpisodeCache):
        self._archive = archive
        self._live = live
        # alert_id -> (plane, latest doc) of every open episode.
        self._open: dict[str, tuple[Plane, dict[str, Any]]] = {}
        # alert_id -> entry the archive has not accepted yet, oldest first.
        self._outbox: dict[str, EpisodeEntry] = {}
        # alert_id -> rejections of its current outbox entry.
        self._rejections: dict[str, int] = {}
        #: Outbox entries dropped since start: over OUTBOX_MAX, rejected
        #: QUARANTINE_AFTER times, or unreadable when restored.
        self.dropped = 0

    # -- EpisodeStore --------------------------------------------------------

    def record_alert_episode(
        self,
        plane: Plane,
        meta: dict[str, Any],
        payload: dict[str, Any],
        *,
        alert_id: str,
        activated_at: float,
        active: bool = True,
        deactivated_at: float | None = None,
    ) -> None:
        """Record an activation (or re-record at a handover) or a deactivation."""
        previous = self._open.get(alert_id)
        doc = alert_episode_doc(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at,
            active=active,
            deactivated_at=deactivated_at,
            # Carried in the document, so an outbox entry or a handover can
            # never lose the segment where the episode began.
            start_flight_id=previous[1].get("start_flight_id") if previous else None,
        )
        if active:
            # Re-recording an ongoing episode (segment handover) must not
            # roll last_updated back to activated_at.
            last_seen = previous[1].get("last_updated") if previous else None
            if isinstance(last_seen, (int, float)) and last_seen > (
                doc.get("last_updated") or 0
            ):
                doc["last_updated"] = last_seen
            self._open[alert_id] = (plane, doc)
        else:
            self._open.pop(alert_id, None)
        plane.track.in_archive = True  # the episode writes the flight's row
        self._write((flight_row(plane), doc))
        self._live.cache_episode(doc)

    def update_active_alert(
        self,
        plane: Plane,
        alert_id: str,
        meta: dict[str, Any],
        payload: dict[str, Any],
        timestamp: float,
    ) -> None:
        """Refresh an open episode; the archive gets it with the next checkpoint."""
        previous = self._open.get(alert_id)
        activated_at = previous[1].get("activated_at") if previous else None
        doc = alert_episode_doc(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at if activated_at is not None else timestamp,
            active=True,
            start_flight_id=previous[1].get("start_flight_id") if previous else None,
        )
        doc["last_updated"] = timestamp
        self._open[alert_id] = (plane, doc)
        self._live.cache_episode(doc)

    # -- archive side --------------------------------------------------------

    def checkpoint_episodes(
        self, flight_ids: Collection[str] | None = None
    ) -> list[EpisodeEntry]:
        """The latest state of the open episodes (on *flight_ids*, the
        flights being checkpointed; None: all). The outbox is not included:
        :meth:`drain` writes it on its own."""
        return [
            (flight_row(plane), doc)
            for plane, doc in self._open.values()
            if flight_ids is None or doc.get("flight_id") in flight_ids
        ]

    def written(self, entries: Sequence[EpisodeEntry]) -> None:
        """*entries* (from :meth:`checkpoint_episodes`) were committed: a
        queued activation or refresh of the same episode is superseded."""
        done = []
        for _flight, doc in entries:
            alert_id = doc.get("alert_id")
            if not isinstance(alert_id, str):
                continue
            queued = self._outbox.get(alert_id)
            if queued is not None and queued[1].get("active"):
                self._forget(alert_id)
                done.append(alert_id)
        if done:
            self._cache("drop", self._live.drop_outbox, done)

    def restore_outbox(self) -> int:
        """Queue the episodes a previous engine left in the Redis outbox
        (it crashed while the archive was down). Returns how many. Entries
        that lack a key the archive needs are dropped (and counted)."""
        restored = 0
        invalid = 0
        for raw in self._live.load_outbox():
            entry = valid_entry(raw)
            if entry is None:
                invalid += 1
                continue
            alert_id = entry[1]["alert_id"]
            if alert_id not in self._outbox:
                self._outbox[alert_id] = entry
                restored += 1
        if invalid:
            self.dropped += invalid
            log.error(
                "Dropped %d unreadable alert episode(s) from the Redis outbox "
                "(counted as episodes_dropped)",
                invalid,
            )
        if restored:
            log.warning(
                "Replaying %d alert episode(s) a previous engine could not archive",
                restored,
            )
        if invalid:
            # The readable ones are written back as they are accepted.
            self._cache("clear", self._live.replace_outbox, dict(self._outbox))
        return restored

    def drain(self, flight_id: str | None = None) -> bool:
        """Write the outbox (only *flight_id*'s entries, if given), each
        rejected entry alone. False when the archive is unavailable; entries
        it rejects for their content do not hold up the rest."""
        entries = [
            entry
            for entry in self._outbox.values()
            if flight_id is None or entry[1].get("flight_id") == flight_id
        ]
        if not entries:
            return True
        result = self._upsert(entries)
        if result == "ok":
            self._accepted(entries)
            return True
        if result == "down":
            return False
        for entry in entries:
            result = self._upsert([entry])
            if result == "down":
                return False
            if result == "ok":
                self._accepted([entry])
            else:
                self._rejected(entry)
        return True

    def open_docs(self, flight_id: str) -> list[dict[str, Any]]:
        """Copies of the open episodes on *flight_id*."""
        return [
            dict(doc)
            for _plane, doc in self._open.values()
            if doc.get("flight_id") == flight_id
        ]

    @property
    def outbox(self) -> Mapping[str, EpisodeEntry]:
        return self._outbox

    def _write(self, entry: EpisodeEntry) -> None:
        alert_id = entry[1]["alert_id"]
        queued = self._outbox.pop(alert_id, None)
        self._rejections.pop(alert_id, None)
        result = self._upsert([entry])
        if result == "ok":
            if queued is not None:
                self._cache("drop", self._live.drop_outbox, [alert_id])
            return
        self._outbox[alert_id] = entry
        if result == "rejected":
            self._rejections[alert_id] = 1
        while len(self._outbox) > OUTBOX_MAX:
            oldest = next(iter(self._outbox))
            self._forget(oldest)
            if not self.dropped:
                log.error(
                    "Unarchived alert episode outbox is full (%d); dropping the "
                    "oldest (counted as episodes_dropped)",
                    OUTBOX_MAX,
                )
            self.dropped += 1
            self._cache("drop", self._live.drop_outbox, [oldest])
        self._cache("save", self._live.save_outbox, {alert_id: entry})

    def _accepted(self, entries: Sequence[EpisodeEntry]) -> None:
        done = []
        for _flight, doc in entries:
            alert_id = doc["alert_id"]
            self._forget(alert_id)
            done.append(alert_id)
        self._cache("drop", self._live.drop_outbox, done)

    def _rejected(self, entry: EpisodeEntry) -> None:
        alert_id = entry[1]["alert_id"]
        count = self._rejections.get(alert_id, 0) + 1
        if count < QUARANTINE_AFTER:
            self._rejections[alert_id] = count
            return
        self._forget(alert_id)
        self.dropped += 1
        log.error(
            "Dropped alert episode %s of %s: the archive rejected it %d times "
            "(counted as episodes_dropped)",
            alert_id,
            entry[1].get("flight_id"),
            count,
        )
        self._cache("drop", self._live.drop_outbox, [alert_id])

    def _forget(self, alert_id: str) -> None:
        self._outbox.pop(alert_id, None)
        self._rejections.pop(alert_id, None)

    def _cache(self, what: str, call: Any, arg: Any) -> None:
        try:
            call(arg)
        except Exception:
            log.exception("Episode outbox %s in the live store failed", what)

    def _upsert(self, entries: list[EpisodeEntry]) -> _Result:
        """``ok``, ``down`` (archive unavailable: retry everything later) or
        ``rejected`` (the entries' content, or a bug: retried one by one)."""
        try:
            return "ok" if self._archive.upsert_episodes(entries) else "down"
        except RowRejected as exc:
            log.warning("Archive rejected %d alert episode(s): %s", len(entries), exc)
            return "rejected"
        except Exception:
            # A bug, not an outage: keep the episode for a bounded retry
            # rather than lose it (and skip the live cache write) with the
            # exception.
            log.exception("Recording %d alert episode(s) failed", len(entries))
            return "rejected"
