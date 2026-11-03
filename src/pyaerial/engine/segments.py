"""Flight segmentation helpers: where the next segment starts, the detached
copy archived for the old one, and trimming the live plane."""

from __future__ import annotations

from dataclasses import replace

from pyaerial.constants import STORE_LAT, STORE_LONG
from pyaerial.models import Plane

# tracking.max_flight_seconds default: longer tracks (ground vehicles, parked
# aircraft) are archived in segments instead of growing in memory forever.
DEFAULT_MAX_FLIGHT_SECONDS = 14_400.0


def next_segment_start(plane: Plane, remember_planes: float) -> tuple[float, bool]:
    """``(new first_packet, keep the last position)`` for a split of *plane*.

    The new flight starts at the last position sample, which both segments
    share, but only when it is recent: after a long stretch without
    positions (Mode S only) it would make the segments overlap by hours, so
    the next segment starts at the last packet without a position.
    """
    track = plane.track
    old_first = track.first_packet
    last_packet = track.last_update
    recv = plane.received_data
    lat, lon = recv.get(STORE_LAT) or [], recv.get(STORE_LONG) or []
    last_position = min(lat[-1].time, lon[-1].time) if lat and lon else None
    position_is_fresh = (
        last_position is not None
        and last_packet - last_position <= remember_planes
        and int(last_position) > int(old_first)
    )
    new_first = (
        last_position
        if position_is_fresh and last_position is not None
        else last_packet
    )
    return new_first, position_is_fresh


def segment_copy(plane: Plane) -> Plane:
    """A detached copy of a plane's series for archiving one segment."""
    track = plane.track
    return Plane(
        info=plane.info.copy(),
        track=replace(
            track,
            position_receivers=dict(track.position_receivers),
            integrity_log=list(track.integrity_log),
        ),
        received_data={k: list(v) for k, v in plane.received_data.items()},
        calculated_data={k: list(v) for k, v in plane.calculated_data.items()},
        active_alerts=list(plane.active_alerts),
    )


def start_new_segment(plane: Plane, new_first: float, *, keep_position: bool) -> None:
    """Trim the live plane to the start of its next segment.

    The last value of every series is kept (speed, heading, ETA, and the
    verdict in effect carry on); a stale last position is dropped so the
    next segment has no sample before its start.
    """
    track = plane.track
    previous_flight_id = plane.flight_id
    for bucket in (plane.received_data, plane.calculated_data):
        for series in bucket.values():
            del series[:-1]
    if not keep_position:
        recv = plane.received_data
        for key in (STORE_LAT, STORE_LONG):
            if key in recv:
                recv[key].clear()
    heard = track.position_receivers
    if heard:
        for stamp in [s for s in heard if s < new_first]:
            del heard[stamp]
    verdicts = track.integrity_log
    if verdicts:
        in_effect = [v for v in verdicts if v.time <= new_first][-1:]
        verdicts[:] = in_effect + [v for v in verdicts if v.time > new_first]
    track.first_packet = new_first
    # A new flight row in the archive, linked to the segment it continues.
    track.previous_flight_id = previous_flight_id
    track.archived_through = None
    track.verdicts_through = None
    track.checkpoint_at = None
