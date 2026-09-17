"""
Plane tracking: deduplication, state updates, and expiry.

Receivers emit raw ``(hex, timestamp)`` pairs; this module deduplicates them,
classifies new messages, and maintains the in-memory plane store.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from pyModeS.util import icao as pms_icao

from pyaerial.classify import ClassifiedMessage, classify
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
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
                classified = classify(
                    msg_hex, self.config.home, last_position=self._last_position(msg_hex)
                )
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
            info = plane[STORE_INFO]
            for key, value in message_data[STORE_INFO].items():
                if key == STORE_ICAO:
                    info[key] = value
                    continue
                if value in ("", None, []) and info.get(key) not in (None, "", []):
                    continue
                info[key] = value

            recv = plane.setdefault(STORE_RECV_DATA, {})
            for field, value in message_data[STORE_RECV_DATA].items():
                datum = Datum(value, timestamp)
                series = recv.setdefault(field, [])
                if not series:
                    series.append(datum)
                    continue
                last = series[-1]
                if last.value == datum.value:
                    if timestamp > last.time:
                        if len(series) >= 2 and series[-2].value == last.value:
                            last.time = timestamp
                        else:
                            series.append(Datum(last.value, timestamp))
                    continue
                if datum.time <= last.time:
                    datum = Datum(datum.value, last.time + 1e-6)
                series.append(datum)

        internal = plane.internal
        if STORE_FIRST_PACKET not in internal:
            internal[STORE_FIRST_PACKET] = timestamp
            internal[STORE_TOTAL_PACKETS] = 0
            internal[STORE_PACKET_TYPE] = defaultdict(int)
        previous = internal.get(STORE_MOST_RECENT_PACKET)
        if previous is None or timestamp > previous:
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

        # Live Redis trims to telemetry_keep_seconds. The in-memory series is
        # the archive source for SQLite on expire, so it is not clipped here.

    def _last_position(self, msg_hex: str) -> tuple[float, float] | None:
        try:
            icao = pms_icao(msg_hex)
        except Exception:
            return None
        if not isinstance(icao, str):
            return None
        plane = self.planes.get(icao) or self.planes.get(icao.lower())
        if plane is None:
            return None
        recv = plane.get(STORE_RECV_DATA) or {}
        lat_series = recv.get(STORE_LAT) or []
        lon_series = recv.get(STORE_LONG) or []
        if not lat_series or not lon_series:
            return None
        return (lat_series[-1].value, lon_series[-1].value)
