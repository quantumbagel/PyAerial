"""
Plane tracking: deduplication, state updates, and expiry.

Receivers emit raw ``(hex, timestamp)`` pairs; this module deduplicates them,
classifies new messages, and maintains the in-memory plane store.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from pyaerial.classify import ClassifiedMessage, classify
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_MOST_RECENT_PACKET,
    STORE_PACKET_TYPE,
    STORE_RECV_DATA,
    STORE_TOTAL_PACKETS,
)
from pyaerial.models import Datum, Plane

log = logging.getLogger("pyaerial.tracker")


class Tracker:
    """In-memory plane store with message deduplication."""

    def __init__(self, config: Config):
        self.config = config
        self.planes: dict[str, Plane] = {}
        # full frame hex -> most recent timestamp we have seen for that exact frame
        self._recent: dict[str, float] = {}

    def ingest(
        self,
        messages: list[tuple[str, float]],
        *,
        receivers: dict[str, str] | None = None,
    ) -> int:
        """Classify and merge ``messages`` into the plane store. Returns count processed."""
        processed = 0
        for msg_hex, timestamp in messages:
            try:
                classified = classify(msg_hex, self.config.home)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                log.debug("Could not classify message %s: %s", msg_hex, exc)
                continue
            if classified is None:
                continue
            receiver = (receivers or {}).get(msg_hex)
            self._merge(classified, timestamp, receiver=receiver)
            processed += 1
        return processed

    def collect_new_messages(
        self, incoming: list[tuple[str, float]]
    ) -> list[tuple[str, float]]:
        """
        Deduplicate ``incoming`` against recently seen messages.

        A message is considered new if we have never seen that exact frame, or
        if the same frame was last seen longer ago than
        ``duplicate_packet_merging`` seconds. Note that ``msg_hex`` here is the
        full 28/14-character frame, so distinct frames from the same aircraft
        (position vs. velocity vs. callsign) are not merged together.
        """
        merge_window = self.config.tracking.duplicate_packet_merging
        to_process: list[tuple[str, float]] = []
        now = time.time()

        for msg_hex, timestamp in sorted(incoming, key=lambda item: item[1]):
            last_seen = self._recent.get(msg_hex)
            if last_seen is None or abs(timestamp - last_seen) > merge_window:
                to_process.append((msg_hex, timestamp))
                self._recent[msg_hex] = timestamp

        # Prune stale entries from the recent index.
        cutoff = now - merge_window
        self._recent = {h: t for h, t in self._recent.items() if t >= cutoff}
        return to_process

    def expired_planes(self, current_time: float | None = None) -> list[str]:
        """Return ICAO ids of planes not updated within ``remember_planes`` seconds."""
        now = current_time or time.time()
        threshold = self.config.tracking.remember_planes
        expired = []
        for icao, plane in self.planes.items():
            last = plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET]
            if now - last > threshold:
                expired.append(icao)
        return expired

    def remove_planes(self, icaos: list[str]) -> list[dict]:
        """Remove and return plane dicts for the given ICAO ids."""
        removed = []
        for icao in icaos:
            plane = self.planes.pop(icao, None)
            if plane is not None:
                removed.append(plane)
        return removed

    def top_planes_summary(self) -> str:
        """Format a status line listing the busiest tracked planes."""
        top_n = self.config.tracking.status_message_top_planes
        if not self.planes or top_n == 0:
            return ""

        by_packets = {
            icao: plane[STORE_INTERNAL][STORE_TOTAL_PACKETS]
            for icao, plane in self.planes.items()
        }
        sorted_planes = sorted(by_packets, key=by_packets.get, reverse=True)
        if top_n > 0:
            sorted_planes = sorted_planes[:top_n]

        parts = []
        advanced = self.config.tracking.advanced_status
        for icao in sorted_planes:
            count = by_packets[icao]
            if not advanced:
                parts.append(f"{icao} ({count})")
                continue
            plane = self.planes[icao]
            callsign = plane[STORE_INFO].get("callsign", "")
            pkt_types = plane[STORE_INTERNAL][STORE_PACKET_TYPE]
            if callsign:
                parts.append(f"{icao}/{callsign} ({count}, {pkt_types})")
            else:
                parts.append(f"{icao} ({count}, {pkt_types})")

        label = "All" if top_n == -1 else f"Top {min(top_n, len(sorted_planes))}"
        return f"{label}: {', '.join(parts)}"

    def _merge(
        self,
        classified: ClassifiedMessage,
        timestamp: float,
        *,
        receiver: str | None = None,
    ) -> None:
        message_data = classified.data
        typecode_cat = classified.typecode_category
        icao = message_data[STORE_INFO][STORE_ICAO]

        if icao not in self.planes:
            plane = Plane.from_mapping(message_data)
            for field, value in list(plane.received_data.items()):
                plane.received_data[field] = [Datum(value, timestamp)]
            self.planes[icao] = plane
        else:
            plane = Plane.from_mapping(self.planes[icao])
            self.planes[icao] = plane
            for key, value in message_data[STORE_INFO].items():
                plane[STORE_INFO][key] = value

            recv = plane.setdefault(STORE_RECV_DATA, {})
            for field, value in message_data[STORE_RECV_DATA].items():
                datum = Datum(value, timestamp)
                series = recv.setdefault(field, [])
                if not series or series[-1].value != datum.value:
                    series.append(datum)
                elif timestamp > series[-1].time:
                    # Same value, newer time: keep the sample current so a
                    # stopped aircraft ages to speed 0 instead of freezing.
                    series[-1].time = timestamp

        internal = plane.internal
        if STORE_FIRST_PACKET not in internal:
            internal[STORE_FIRST_PACKET] = timestamp
            internal[STORE_TOTAL_PACKETS] = 0
            internal[STORE_PACKET_TYPE] = defaultdict(int)
        internal[STORE_MOST_RECENT_PACKET] = timestamp
        internal[STORE_TOTAL_PACKETS] += 1
        pkt_types = internal[STORE_PACKET_TYPE]
        if not isinstance(pkt_types, defaultdict):
            pkt_types = defaultdict(int, pkt_types)
            internal[STORE_PACKET_TYPE] = pkt_types
        pkt_types[typecode_cat] += 1

        if receiver:
            plane[STORE_INFO]["receiver"] = receiver
            seen = plane[STORE_INFO].setdefault("receivers", [])
            if receiver not in seen:
                seen.append(receiver)

        self._trim_series(plane, timestamp)

    def _trim_series(self, plane: Plane, now: float) -> None:
        keep = self.config.tracking.telemetry_keep_seconds
        if keep <= 0:
            return
        cutoff = now - keep
        for bucket in (STORE_RECV_DATA, STORE_CALC_DATA):
            fields = plane.get(bucket)
            if not isinstance(fields, dict):
                continue
            for field, series in list(fields.items()):
                if not series:
                    continue
                trimmed = [datum for datum in series if datum.time >= cutoff]
                if not trimmed:
                    trimmed = series[-1:]
                fields[field] = trimmed
