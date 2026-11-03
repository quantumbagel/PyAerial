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
from pyaerial.store.codec import alert_episode_doc, flight_row

log = logging.getLogger("pyaerial.store")

# (flight_row, alert_doc): an episode as ArchiveWriter.upsert_episodes takes it.
EpisodeEntry = tuple[dict[str, Any], dict[str, Any]]
# Episode writes waiting for the archive while it is down, latest state per
# alert_id (~1 KB each). A busy site raises a few hundred alerts an hour, so
# this covers days of outage; beyond it the oldest are dropped (counted).
OUTBOX_MAX = 10_000


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


class EpisodeRecorder:
    """Records alert episodes in the archive at once, then in the live cache.

    - Activation and deactivation: upserted into the archive with the
      flight's row (one small transaction), then written to the live store.
    - While-active refreshes (ETA, position, ``last_updated``): kept in
      memory per open episode, written to the archive with the next
      checkpoint (:meth:`checkpoint_episodes`), and to the live store now.
    - A write the archive refuses (outage) waits in the outbox, latest state
      per alert_id (at most OUTBOX_MAX), and is re-sent with every
      checkpoint until it commits. It is copied to Redis while it waits, so
      the next engine replays it after a crash (:meth:`restore_outbox`).

    Tick thread only.
    """

    def __init__(self, archive: EpisodeArchive, live: EpisodeCache):
        self._archive = archive
        self._live = live
        # alert_id -> (plane, latest doc) of every open episode.
        self._open: dict[str, tuple[Plane, dict[str, Any]]] = {}
        # alert_id -> entry the archive has not accepted yet, oldest first.
        self._outbox: dict[str, EpisodeEntry] = {}
        #: Outbox entries dropped over OUTBOX_MAX since start.
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
        doc = alert_episode_doc(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at,
            active=active,
            deactivated_at=deactivated_at,
        )
        if active:
            # Re-recording an ongoing episode (segment handover) must not
            # roll last_updated back to activated_at.
            previous = self._open.get(alert_id)
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
        )
        doc["last_updated"] = timestamp
        self._open[alert_id] = (plane, doc)
        self._live.cache_episode(doc)

    # -- archive side --------------------------------------------------------

    def checkpoint_episodes(
        self, flight_ids: Collection[str] | None = None
    ) -> list[EpisodeEntry]:
        """Outbox entries, then the latest state of the open episodes (on
        *flight_ids*, the flights being checkpointed; None: all)."""
        return [
            *self._outbox.values(),
            *(
                (flight_row(plane), doc)
                for plane, doc in self._open.values()
                if flight_ids is None or doc.get("flight_id") in flight_ids
            ),
        ]

    def written(self, entries: Sequence[EpisodeEntry]) -> None:
        """*entries* (from :meth:`checkpoint_episodes`) were committed."""
        done = []
        for entry in entries:
            alert_id = entry[1].get("alert_id")
            if alert_id is not None and self._outbox.get(alert_id) is entry:
                del self._outbox[alert_id]
                done.append(alert_id)
        if done:
            self._cache("drop", self._live.drop_outbox, done)

    def restore_outbox(self) -> int:
        """Queue the episodes a previous engine left in the Redis outbox
        (it crashed while the archive was down). Returns how many."""
        restored = 0
        for entry in self._live.load_outbox():
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], dict)
                and isinstance(entry[1], dict)
                and entry[1].get("alert_id")
                and entry[1]["alert_id"] not in self._outbox
            ):
                self._outbox[entry[1]["alert_id"]] = (entry[0], entry[1])
                restored += 1
        if restored:
            log.warning(
                "Replaying %d alert episode(s) a previous engine could not archive",
                restored,
            )
        return restored

    def drain(self) -> bool:
        """Re-send the outbox now. True when it is empty afterwards."""
        if not self._outbox:
            return True
        entries = list(self._outbox.values())
        if self._upsert(entries):
            self.written(entries)
        return not self._outbox

    def open_on(self, alert_id: str) -> str | None:
        """The flight an open episode is on now (None: not open)."""
        entry = self._open.get(alert_id)
        return entry[1].get("flight_id") if entry else None

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
        if self._upsert([entry]):
            if queued is not None:
                self._cache("drop", self._live.drop_outbox, [alert_id])
            return
        self._outbox[alert_id] = entry
        while len(self._outbox) > OUTBOX_MAX:
            oldest = next(iter(self._outbox))
            del self._outbox[oldest]
            if not self.dropped:
                log.error(
                    "Unarchived alert episode outbox is full (%d); dropping the "
                    "oldest (counted as episodes_dropped)",
                    OUTBOX_MAX,
                )
            self.dropped += 1
            self._cache("drop", self._live.drop_outbox, [oldest])
        self._cache("save", self._live.save_outbox, {alert_id: entry})

    def _cache(self, what: str, call: Any, arg: Any) -> None:
        try:
            call(arg)
        except Exception:
            log.exception("Episode outbox %s in the live store failed", what)

    def _upsert(self, entries: list[EpisodeEntry]) -> bool:
        try:
            return self._archive.upsert_episodes(entries)
        except Exception:
            # A bug, not an outage: keep the episode for retry rather than
            # lose it (and skip the live cache write) with the exception.
            log.exception("Recording %d alert episode(s) failed", len(entries))
            return False
