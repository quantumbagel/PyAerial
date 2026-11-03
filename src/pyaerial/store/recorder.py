"""Alert episodes into the archive (the record) and the live store (the cache).

:class:`EpisodeRecorder` is the tracking engine's
:class:`~pyaerial.store.live.EpisodeStore`: the alert engine records each
activation and deactivation through it, in the order it notifies.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pyaerial.models import Plane
from pyaerial.store.codec import alert_episode_doc, flight_row
from pyaerial.store.live.protocols import EpisodeStore

log = logging.getLogger("pyaerial.store")

# (flight_row, alert_doc): an episode as ArchiveWriter.upsert_episodes takes it.
EpisodeEntry = tuple[dict[str, Any], dict[str, Any]]


class EpisodeArchive(Protocol):
    """The episode side of :class:`~pyaerial.store.ArchiveWriter`."""

    def upsert_episodes(
        self, episodes: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
    ) -> bool: ...


class EpisodeRecorder:
    """Records alert episodes in the archive at once, then in the live cache.

    - Activation and deactivation: upserted into the archive with the
      flight's row (one small transaction), then written to the live store.
    - While-active refreshes (ETA, position, ``last_updated``): kept in
      memory per open episode, written to the archive with the next
      checkpoint (:meth:`checkpoint_episodes`), and to the live store now.
    - A write the archive refuses (outage) waits in the outbox, latest state
      per alert_id, and is re-sent with every checkpoint until it commits.

    Tick thread only.
    """

    def __init__(self, archive: EpisodeArchive, live: EpisodeStore):
        self._archive = archive
        self._live = live
        # alert_id -> (plane, latest doc) of every open episode.
        self._open: dict[str, tuple[Plane, dict[str, Any]]] = {}
        # alert_id -> entry the archive has not accepted yet.
        self._outbox: dict[str, EpisodeEntry] = {}

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
        self._write((flight_row(plane), doc))
        self._live.record_alert_episode(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at,
            active=active,
            deactivated_at=deactivated_at,
        )

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
        self._live.update_active_alert(plane, alert_id, meta, payload, timestamp)

    def get_alerts(
        self,
        *,
        since: float = 0.0,
        flight_id: str | None = None,
        rule: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        return self._live.get_alerts(
            since=since, flight_id=flight_id, rule=rule, active_only=active_only
        )

    # -- archive side --------------------------------------------------------

    def checkpoint_episodes(self) -> list[EpisodeEntry]:
        """Outbox entries, then the latest state of every open episode."""
        return [
            *self._outbox.values(),
            *((flight_row(plane), doc) for plane, doc in self._open.values()),
        ]

    def written(self, entries: Sequence[EpisodeEntry]) -> None:
        """*entries* (from :meth:`checkpoint_episodes`) were committed."""
        for entry in entries:
            alert_id = entry[1].get("alert_id")
            if alert_id is not None and self._outbox.get(alert_id) is entry:
                del self._outbox[alert_id]

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
        self._outbox.pop(alert_id, None)
        if not self._upsert([entry]):
            self._outbox[alert_id] = entry

    def _upsert(self, entries: list[EpisodeEntry]) -> bool:
        try:
            return self._archive.upsert_episodes(entries)
        except Exception:
            # A bug, not an outage: keep the episode for retry rather than
            # lose it (and skip the live cache write) with the exception.
            log.exception("Recording %d alert episode(s) failed", len(entries))
            return False
