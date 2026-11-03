"""In-memory flight tracker managing deduplication, telemetry buffers, and flight lifecycles."""

from __future__ import annotations

import logging
import time
from bisect import bisect_left
from operator import attrgetter
from typing import TYPE_CHECKING

from pyModeS.position import airborne_position_pair

from pyaerial.classify import (
    CAT_AIRBORNE_BARO,
    CAT_AIRBORNE_GNSS,
    CAT_IDENT,
    CAT_SURFACE,
    CAT_VELOCITY,
    ClassifiedMessage,
    airborne_cpr,
    classify,
    max_jump_deg,
)
from pyaerial.config.schema import Config
from pyaerial.constants import STORE_LAT, STORE_LONG
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState
from pyaerial.receivers.frames import es_icao

if TYPE_CHECKING:
    from pyaerial.receivers.positions import ReceiverPositions

log = logging.getLogger("pyaerial.tracker")


# After this many consecutive position fixes fail the jump gate, the last
# accepted fix is presumed bad and the gate is reset (re-anchor from an
# even/odd pair).
_GATE_RESET_AFTER = 5

# Even/odd airborne frames further apart than this may straddle a CPR zone
# (DO-260B global decoding window).
_CPR_PAIR_SECONDS = 10.0

# A last fix older than this is no longer a safe local-decode reference: at
# MAX_SPEED_MPS the aircraft may have moved half of the 180 NM local range.
_LOCAL_REF_SECONDS = 180.0


# Status-line labels for the classify packet-type buckets, in display order.
_PACKET_TYPE_LABELS = (
    (CAT_AIRBORNE_BARO, "pos"),
    (CAT_AIRBORNE_GNSS, "gnss pos"),
    (CAT_SURFACE, "surface"),
    (CAT_VELOCITY, "vel"),
    (CAT_IDENT, "id"),
)


def _format_packet_types(counts: dict[int, int]) -> str:
    """``pos 81, vel 2, id 2`` from a ``{category: count}`` mapping."""
    return ", ".join(
        f"{label} {counts[category]}"
        for category, label in _PACKET_TYPE_LABELS
        if counts.get(category)
    )


def _has_position_sample(plane: Plane, timestamp: float) -> bool:
    """Whether a latitude or longitude sample is stored at exactly *timestamp*."""
    recv = plane.received_data
    for field in (STORE_LAT, STORE_LONG):
        series = recv.get(field) or []
        index = bisect_left(series, timestamp, key=_datum_time)
        if index < len(series) and series[index].time == timestamp:
            return True
    return False


_datum_time = attrgetter("time")


def _apply_identity(info: AircraftInfo, classified: ClassifiedMessage) -> None:
    """Identification fields of *classified*; a blank callsign keeps the old one."""
    if classified.plane_category is not None:
        info.plane_category = classified.plane_category
    if classified.callsign:
        info.callsign = classified.callsign


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
        # icao -> CPR parity (1 = odd) -> newest airborne (lat, lon, time) raw
        # CPR fields, for global even/odd decoding
        self._cpr_frames: dict[str, dict[int, tuple[int, int, float]]] = {}

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
        latest: float | None = None
        for msg_hex, timestamp in messages:
            icao = es_icao(msg_hex)
            if icao is None:
                continue
            if latest is None or timestamp > latest:
                latest = timestamp
            typecode = int(msg_hex[8:10], 16) >> 3
            is_position = 5 <= typecode <= 18 or 20 <= typecode <= 22
            cpr = airborne_cpr(msg_hex)
            pair_position = (
                self._pair_position(icao, cpr, timestamp) if cpr is not None else None
            )
            last_position: tuple[float, float] | None = None
            jump = max_jump_deg(None)
            reset_gate = False
            stale_fix = False
            if is_position:
                last_fix = self._fix_for(icao)
                if last_fix is not None:
                    stale_fix = timestamp - last_fix[2] > _LOCAL_REF_SECONDS
                    if self._gate_rejects.get(icao, 0) >= _GATE_RESET_AFTER:
                        reset_gate = True
                    else:
                        last_position = (last_fix[0], last_fix[1])
                        jump = max_jump_deg(timestamp - last_fix[2])
                if (
                    cpr is not None
                    and pair_position is None
                    and (last_position is None or stale_fix)
                ):
                    # Local decoding aliases by a whole CPR zone (~360 NM)
                    # when the reference is over 180 NM off, and that fix would
                    # anchor every later decode. Without a recent fix (first
                    # fix, gate reset, long gap) an airborne fix needs a pair.
                    continue
                if reset_gate:
                    log.info(
                        "Resetting position gate for %s after %d rejected fixes",
                        icao,
                        self._gate_rejects[icao],
                    )
                    self._gate_rejects.pop(icao, None)

            def _rejected(icao: str = icao) -> None:
                self._gate_rejects[icao] = self._gate_rejects.get(icao, 0) + 1
                plane = self.planes.get(icao)
                if plane is not None:
                    plane.track.gate_rejects += 1

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
                    pair_position=pair_position,
                )
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                log.debug("Could not classify message %s: %s", msg_hex, exc)
                continue
            if classified is None:
                continue
            if STORE_LAT in classified.values:
                self._gate_rejects.pop(icao, None)
            self._merge(classified, timestamp, heard=heard)
            processed += 1
        if latest is not None and self._cpr_frames:
            cutoff = latest - _CPR_PAIR_SECONDS
            self._cpr_frames = {
                icao: frames
                for icao, frames in self._cpr_frames.items()
                if max(frame[2] for frame in frames.values()) >= cutoff
            }
        return processed

    def _pair_position(
        self, icao: str, cpr: tuple[int, int, int], timestamp: float
    ) -> tuple[float, float] | None:
        """Record an airborne CPR frame; globally decode it if its partner is fresh.

        Returns this frame's position from the even/odd pair, which (unlike a
        local decode) needs no reference within 180 NM.
        """
        odd, lat_raw, lon_raw = cpr
        frames = self._cpr_frames.setdefault(icao, {})
        held = frames.get(odd)
        if held is None or timestamp >= held[2]:
            frames[odd] = (lat_raw, lon_raw, timestamp)
        other = frames.get(1 - odd)
        if other is None or abs(timestamp - other[2]) > _CPR_PAIR_SECONDS:
            return None
        even, odd_frame = (
            (other, (lat_raw, lon_raw)) if odd else ((lat_raw, lon_raw), other)
        )
        return airborne_position_pair(
            even[0], even[1], odd_frame[0], odd_frame[1], even_is_newer=not odd
        )

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
        now = time.time() if current_time is None else current_time
        threshold = self.config.tracking.remember_planes
        expired = []
        for icao, plane in self.planes.items():
            if now - plane.track.last_update > threshold:
                expired.append(icao)
        return expired

    def remove_planes(self, icaos: list[str]) -> list[Plane]:
        """Remove and return the planes for the given ICAO ids."""
        removed = []
        for icao in icaos:
            self._gate_rejects.pop(icao, None)
            self._cpr_frames.pop(icao, None)
            plane = self.planes.pop(icao, None)
            if plane is not None:
                removed.append(plane)
        return removed

    def top_planes_summary(self) -> str:
        """Format a status line listing the busiest tracked planes."""
        top_n = self.config.tracking.status_message_top_planes
        if not self.planes or top_n == 0:
            return ""

        by_packets = {icao: plane.track.packets for icao, plane in self.planes.items()}
        sorted_planes = sorted(by_packets, key=by_packets.__getitem__, reverse=True)
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
            callsign = plane.info.callsign
            pkt_types = _format_packet_types(plane.track.packet_types)
            detail = f"{count}, {pkt_types}" if pkt_types else f"{count}"
            if callsign:
                parts.append(f"{icao}/{callsign} ({detail})")
            else:
                parts.append(f"{icao} ({detail})")

        label = "All" if top_n == -1 else f"Top {min(top_n, len(sorted_planes))}"
        return f"{label}: {', '.join(parts)}"

    def _merge(
        self,
        classified: ClassifiedMessage,
        timestamp: float,
        *,
        heard: tuple[str, ...] = (),
    ) -> None:
        icao = classified.icao
        values = classified.values

        # Position sample times dropped from the series (run end moved on).
        replaced: list[float] = []
        plane = self.planes.get(icao)
        if plane is None:
            info = AircraftInfo(icao=icao)
            _apply_identity(info, classified)
            plane = Plane(
                info=info,
                track=TrackState(first_packet=timestamp, last_update=timestamp),
                received_data={
                    field: [Datum(value, timestamp)] for field, value in values.items()
                },
            )
            self.planes[icao] = plane
        else:
            _apply_identity(plane.info, classified)
            recv = plane.received_data
            for field, value in values.items():
                datum = Datum(value, timestamp)
                series = recv.setdefault(field, [])
                if not series:
                    series.append(datum)
                    continue
                last = series[-1]
                if last.value == datum.value:
                    if timestamp > last.time:
                        if len(series) >= 2 and series[-2].value == last.value:
                            if field in (STORE_LAT, STORE_LONG):
                                replaced.append(last.time)
                            last.time = timestamp
                        else:
                            series.append(Datum(last.value, timestamp))
                    continue
                if datum.time <= last.time:
                    continue
                series.append(datum)

        track = plane.track
        if timestamp > track.last_update:
            track.last_update = timestamp
        track.packets += 1
        category = classified.typecode_category
        track.packet_types[category] = track.packet_types.get(category, 0) + 1

        if heard:
            info = plane.info
            info.receiver = heard[0]
            if info.receivers is None:
                info.receivers = []
            seen = info.receivers
            seen.extend(name for name in heard if name not in seen)
            if STORE_LAT in values and _has_position_sample(plane, timestamp):
                track.position_receivers[timestamp] = heard
        # Keep the map to sample times only (archive provenance lookups), or a
        # repeated fix grows it by one entry per frame.
        position_receivers = track.position_receivers
        if position_receivers:
            for stale in replaced:
                if stale in position_receivers and not _has_position_sample(
                    plane, stale
                ):
                    del position_receivers[stale]

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
        recv = plane.received_data
        lat_series = recv.get(STORE_LAT)
        lon_series = recv.get(STORE_LONG)
        if not lat_series or not lon_series:
            return None
        lat = lat_series[-1]
        return (lat.value, lon_series[-1].value, lat.time)
