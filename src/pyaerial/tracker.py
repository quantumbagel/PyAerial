"""In-memory flight tracker managing deduplication, telemetry buffers, and flight lifecycles."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from pyaerial.classify import ClassifiedMessage, classify, max_jump_deg
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
    STORE_POSITION_RECEIVERS,
    STORE_RECV_DATA,
    STORE_TOTAL_PACKETS,
)
from pyaerial.models import Datum, Plane

if TYPE_CHECKING:
    from pyaerial.receivers.positions import ReceiverPositions

log = logging.getLogger("pyaerial.tracker")

# Internal counter of position fixes the CPR jump gate rejected (evidence of
# two transmitters sharing an ICAO address).
STORE_GATE_REJECTS = "gate_rejects"

# After this many consecutive position fixes fail the jump gate, the last
# accepted fix is presumed bad and the gate is reset (decode from home).
_GATE_RESET_AFTER = 5


def es_icao(msg_hex: str) -> str | None:
    """ICAO of a DF17/18 extended squitter, or ``None`` for any other frame.

    Reads the DF from the first byte so short Mode S replies never reach the
    CRC-based address recovery or the ADS-B decoder.
    """
    if len(msg_hex) != 28:
        return None
    try:
        df = int(msg_hex[:2], 16) >> 3
    except ValueError:
        return None
    if df not in (17, 18):
        return None
    return msg_hex[2:8].upper()


class Tracker:
    """In-memory plane store with message deduplication."""

    def __init__(
        self,
        config: Config,
        *,
        positions: ReceiverPositions | None = None,
    ):
        self.config = config
        self.positions = positions
        self.planes: dict[str, Plane] = {}
        # full frame hex -> most recent timestamp we have seen for that exact frame
        self._recent: dict[str, float] = {}
        # icao -> consecutive position fixes rejected by the CPR jump gate
        self._gate_rejects: dict[str, int] = {}

    def ingest(
        self,
        messages: list[tuple[str, float]],
        *,
        receivers: dict[str, tuple[str, ...]] | None = None,
    ) -> int:
        """Classify and merge ``messages`` into the plane store; returns the count.

        *receivers* maps a frame's hex to every receiver that heard it, the
        strongest copy's first.
        """
        processed = 0
        receivers = receivers or {}
        for msg_hex, timestamp in messages:
            icao = es_icao(msg_hex)
            if icao is None:
                continue
            last_fix = self._fix_for(icao)
            last_position: tuple[float, float] | None = None
            jump = max_jump_deg(None)
            if last_fix is not None and icao is not None:
                if self._gate_rejects.get(icao, 0) >= _GATE_RESET_AFTER:
                    log.info(
                        "Resetting position gate for %s after %d rejected fixes",
                        icao,
                        self._gate_rejects[icao],
                    )
                    self._gate_rejects.pop(icao, None)
                else:
                    last_position = (last_fix[0], last_fix[1])
                    jump = max_jump_deg(timestamp - last_fix[2])

            def _rejected(icao: str = icao) -> None:
                self._gate_rejects[icao] = self._gate_rejects.get(icao, 0) + 1
                plane = self.planes.get(icao)
                if plane is not None:
                    internal = plane.setdefault(STORE_INTERNAL, {})
                    internal[STORE_GATE_REJECTS] = internal.get(STORE_GATE_REJECTS, 0) + 1

            heard = receivers.get(msg_hex) or ()
            receiver = heard[0] if heard else None
            reference = None
            if self.positions is not None:
                antenna = self.positions.get(receiver)
                reference = (antenna.latitude, antenna.longitude)
            try:
                classified = classify(
                    msg_hex,
                    self.config.home,
                    last_position=last_position,
                    jump_deg=jump,
                    on_implausible=_rejected,
                    icao=icao,
                    reference=reference,
                )
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                log.debug("Could not classify message %s: %s", msg_hex, exc)
                continue
            if classified is None:
                continue
            if STORE_LAT in classified.data.get(STORE_RECV_DATA, {}):
                self._gate_rejects.pop(icao, None)
            self._merge(classified, timestamp, heard=heard)
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
            self._gate_rejects.pop(icao, None)
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
        heard: tuple[str, ...] = (),
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
                    continue
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

        if heard:
            plane[STORE_INFO]["receiver"] = heard[0]
            seen = plane[STORE_INFO].setdefault("receivers", [])
            seen.extend(name for name in heard if name not in seen)
            if STORE_LAT in message_data[STORE_RECV_DATA]:
                internal.setdefault(STORE_POSITION_RECEIVERS, {})[timestamp] = heard

        # Live Redis trims to telemetry_keep_seconds. The in-memory series is
        # the archive source for SQLite on expire, so it is not clipped here.

    def _last_position(self, msg_hex: str) -> tuple[float, float] | None:
        icao = es_icao(msg_hex)
        fix = self._fix_for(icao) if icao is not None else None
        return (fix[0], fix[1]) if fix is not None else None

    def _fix_for(self, icao: str) -> tuple[float, float, float] | None:
        """``(lat, lon, time)`` of the aircraft's newest position, if any."""
        plane = self.planes.get(icao) or self.planes.get(icao.lower())
        if plane is None:
            return None
        recv = plane.get(STORE_RECV_DATA) or {}
        lat_series = recv.get(STORE_LAT) or []
        lon_series = recv.get(STORE_LONG) or []
        if not lat_series or not lon_series:
            return None
        lat = lat_series[-1]
        return (lat.value, lon_series[-1].value, lat.time)
