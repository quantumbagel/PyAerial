"""Plane <-> document conversion: the one place that knows both shapes.

The in-memory :class:`~pyaerial.models.Plane` becomes the live Redis flight
document and its telemetry points, the archive's flight row, telemetry rows
and integrity rows; a live document (plus its telemetry points), an archived
flight row (plus its telemetry), or a set of stray alert documents becomes a
``Plane`` again for archiving. Field names
and value encodings here are wire formats: changing one is a contract change.
"""

from __future__ import annotations

import math
import time
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_ALT,
    STORE_CALLSIGN,
    STORE_GNSS_HEIGHT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import (
    AircraftInfo,
    Datum,
    Plane,
    Series,
    TrackState,
    Verdict,
    iter_telemetry_samples,
    sample_provenance,
)

# Seconds a GNSS height stays attached to later position samples.
GNSS_HEIGHT_MAX_AGE = 10.0

# (timestamp, lat, lon, alt, speed, heading), see iter_telemetry_samples.
Sample = tuple[float, float, float, float | None, float | None, float | None]

# AircraftInfo field -> accepted document value type(s).
_INFO_TYPES: dict[str, type | tuple[type, ...]] = {
    "icao": str,
    "callsign": str,
    "plane_category": list,
    "receiver": str,
    "receivers": list,
    "integrity": dict,
    "registration": str,
    "model": str,
    "owner": str,
    "country": str,
    "aircraft_type": str,
    "manufacturer": str,
    "manufacturer_name": str,
    "operator_callsign": str,
    "photo_url": str,
    "metadata_resolved": bool,
}


# --- info document ---------------------------------------------------------


def info_doc(info: AircraftInfo) -> dict[str, Any]:
    """The ``info`` object of the live flight document and the archive row.

    Keys in the order they were first set; values are shared, not copied.
    """
    doc: dict[str, Any] = {}
    extra = info.extra
    for key in info.doc_order():
        value = getattr(info, key) if key in _INFO_TYPES else None
        if value is None:
            if key not in extra:
                continue
            value = extra[key]
        doc[key] = value
    return doc


def info_from_doc(raw: Mapping[str, Any], icao: str) -> AircraftInfo:
    """Rebuild :class:`AircraftInfo` from an ``info`` document.

    *icao* (lowercased by the caller) replaces the document's. Keys this
    version does not model, and values of an unexpected type, are kept in
    ``extra`` so the archived document matches the live one.
    """
    info = AircraftInfo(icao=icao)
    for key, value in raw.items():
        if key == "icao":
            continue
        expected = _INFO_TYPES.get(key)
        if expected is not None and value is not None and isinstance(value, expected):
            setattr(info, key, value)
        else:
            info.set_extra(key, value)
    return info


def info_text_fields(info: AircraftInfo) -> dict[str, str]:
    """Searchable text columns of a flight (live document and archive row)."""
    return {
        "callsign": info.callsign or "",
        "model": info.model or "",
        "owner": info.owner or "",
        "country": info.country or "",
        "aircraft_type": info.aircraft_type or "",
        "registration": info.registration or "",
    }


def info_json(info: AircraftInfo) -> str:
    """The archive's ``flights.info`` column."""
    return json_dumps(info_doc(info), default=str)


# --- live Redis ------------------------------------------------------------


def live_flight_doc(plane: Plane) -> dict[str, Any]:
    """``live:flight:{flight_id}``."""
    info = plane.info
    return {
        "flight_id": plane.flight_id,
        "icao": plane.icao,
        "status": "live",
        "active_alerts": plane.active_alerts or [],
        "start_time": plane.track.first_packet,
        "end_time": plane.track.last_update,
        **info_text_fields(info),
        "info": info_doc(info),
    }


def live_telemetry_point(plane: Plane, sample: Sample, icao: str) -> dict[str, Any]:
    """One ``live:telemetry:{flight_id}`` member."""
    timestamp, lat, lon = sample[0], sample[1], sample[2]
    point: dict[str, Any] = {
        "icao": icao,
        "timestamp": timestamp,
        "latitude": lat,
        "longitude": lon,
    }
    _add_sample_fields(point, plane, sample)
    return point


def live_doc_icao(doc: Mapping[str, Any]) -> str:
    """Lowercased ICAO of a live flight document ('' when it has none)."""
    info = doc.get("info")
    raw_icao = info.get("icao") if isinstance(info, dict) else None
    return str(doc.get("icao") or raw_icao or "").lower()


def plane_from_live(
    doc: Mapping[str, Any], icao: str, points: Sequence[Mapping[str, Any]]
) -> Plane:
    """A plane rebuilt from its live document and telemetry points, to archive.

    The series are those of the points (clipped to the live window); receivers
    and verdicts come from each point's provenance fields.
    """
    raw_info = doc.get("info")
    info = info_from_doc(raw_info if isinstance(raw_info, dict) else {}, icao)
    return _plane_from_points(
        info,
        doc.get("start_time"),
        doc.get("end_time"),
        points,
        doc.get("active_alerts") or [],
    )


def plane_from_rows(
    flight: Mapping[str, Any], points: Sequence[Mapping[str, Any]]
) -> Plane:
    """A plane rebuilt from an archived flight row and its telemetry points.

    For decisions on an interrupted flight (retention) from what the archive
    holds: *flight* has the ``flights`` columns with ``info`` decoded,
    *points* the decoded telemetry rows, oldest first.
    """
    raw_info = flight.get("info")
    icao = str(flight.get("icao") or "").lower()
    info = info_from_doc(raw_info if isinstance(raw_info, dict) else {}, icao)
    plane = _plane_from_points(
        info, flight.get("start_time"), flight.get("end_time"), points, []
    )
    previous = flight.get("previous_flight_id")
    plane.track.previous_flight_id = previous if isinstance(previous, str) else None
    return plane


def _plane_from_points(
    info: AircraftInfo,
    start: Any,
    end: Any,
    points: Sequence[Mapping[str, Any]],
    active_alerts: list[dict[str, Any]],
) -> Plane:
    recv: Series = {}
    calc: Series = {}

    def _append(series: Series, key: str, value: Any, ts: float) -> None:
        if value is None:
            return
        series.setdefault(key, []).append(Datum(value, ts))

    heard_by: dict[float, tuple[str, ...]] = {}
    verdicts: list[Verdict] = []
    for point in points:
        ts = point.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        _append(recv, STORE_LAT, point.get("latitude"), ts)
        _append(recv, STORE_LONG, point.get("longitude"), ts)
        _append(recv, STORE_ALT, point.get("altitude"), ts)
        _append(recv, STORE_GNSS_HEIGHT, point.get("gnss_height"), ts)
        _append(calc, STORE_HORIZ_SPEED, point.get("speed"), ts)
        _append(calc, STORE_HEADING, point.get("heading"), ts)
        receivers = point.get("receivers")
        if isinstance(receivers, list) and receivers:
            heard_by[ts] = tuple(str(name) for name in receivers)
        score = point.get("integrity_score")
        status = point.get("integrity_status")
        # An ``unknown`` verdict (no check had evidence) has no score.
        if isinstance(status, str) and (
            score is None
            or (isinstance(score, (int, float)) and not isinstance(score, bool))
        ):
            score = float(score) if score is not None else None
            flags = point.get("integrity_flags") or []
            flagged = tuple(str(name) for name in flags)
            last = verdicts[-1] if verdicts else None
            if last is None or (last.score, last.status, last.flagged) != (
                score,
                status,
                flagged,
            ):
                verdicts.append(Verdict(ts, score, status, flagged))
    if not isinstance(start, (int, float)):
        start = points[0]["timestamp"] if points else time.time()
    if not isinstance(end, (int, float)):
        end = points[-1]["timestamp"] if points else start
    return Plane(
        info=info,
        track=TrackState(
            first_packet=start,
            last_update=end,
            position_receivers=heard_by,
            integrity_log=verdicts,
        ),
        received_data=recv,
        calculated_data=calc,
        active_alerts=active_alerts,
    )


def plane_from_alert_docs(
    flight_id: str, docs: Sequence[Mapping[str, Any]]
) -> Plane | None:
    """Minimal plane for alerts whose flight never reached ``live:flights``.

    Rebuilt from the alert docs alone: ICAO and first packet come from the
    flight id (``{icao}-{first_packet}``) so ``Plane.flight_id`` matches,
    the position from the last alert update.
    """
    icao, _, first_text = flight_id.rpartition("-")
    try:
        first = float(int(first_text))
    except ValueError:
        return None
    if not icao:
        return None
    stamps = [
        value
        for doc in docs
        for value in (doc.get("activated_at"), doc.get("last_updated"))
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    last = max([first, *stamps])
    plane = Plane(
        info=AircraftInfo(
            icao=icao,
            callsign=next((d["callsign"] for d in docs if d.get("callsign")), ""),
        ),
        track=TrackState(first_packet=first, last_update=last),
    )
    newest = max(
        docs, key=lambda d: d.get("last_updated") or d.get("activated_at") or 0
    )
    coords = (newest.get("position") or {}).get("coordinates") or []
    if len(coords) >= 2 and all(
        isinstance(c, (int, float)) and math.isfinite(c) for c in coords[:2]
    ):
        plane.received_data = {
            STORE_LAT: [Datum(coords[1], last)],
            STORE_LONG: [Datum(coords[0], last)],
        }
    return plane


def alert_episode_doc(
    plane: Plane,
    meta: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    alert_id: str,
    activated_at: float,
    active: bool,
    deactivated_at: float | None = None,
) -> dict[str, Any]:
    """One alert episode (live ``live:alerts:*`` member, archive ``alerts`` row).

    *meta* and *payload* are the alert engine's notification fields; only
    named keys are copied.
    """
    return {
        "alert_id": alert_id,
        "flight_id": plane.flight_id,
        "icao": meta[STORE_ICAO].lower(),
        "callsign": meta.get(STORE_CALLSIGN) or "",
        "zone": meta.get(ALERT_CAT_ZONE, ""),
        "rule": meta.get(ALERT_CAT_TYPE, ""),
        "active": active,
        "activated_at": activated_at,
        "deactivated_at": deactivated_at,
        "eta": meta.get(ALERT_CAT_ETA),
        "reason": meta.get(ALERT_CAT_REASON),
        "last_updated": deactivated_at or activated_at,
        "position": {
            "type": "Point",
            "coordinates": [payload.get(STORE_LONG), payload.get(STORE_LAT)],
        },
        "altitude": payload.get(STORE_ALT),
    }


# --- archive ---------------------------------------------------------------


def build_telemetry_docs(
    plane: Plane,
    flight_id: str,
    icao: str,
    *,
    since: float | None = None,
    until: float | None = None,
) -> list[dict[str, Any]]:
    """Archive telemetry documents from a plane's in-memory time series.

    Each sample carries the receivers that heard it and the integrity verdict
    in effect when it arrived (see :func:`pyaerial.models.sample_provenance`).
    ``since`` / ``until`` select the samples with ``since < t <= until``
    (a checkpoint slice); the default is the whole flight.
    """
    docs: list[dict[str, Any]] = []
    used_ts: set[float] = set()
    for sample in iter_telemetry_samples(plane, since=since):
        if until is not None and sample[0] > until:
            break
        stamp = sample[0]
        while stamp in used_ts:
            stamp += 1e-6
        used_ts.add(stamp)
        doc: dict[str, Any] = {
            "flight_id": flight_id,
            "icao": icao,
            "timestamp": stamp,
            "latitude": sample[1],
            "longitude": sample[2],
        }
        _add_sample_fields(doc, plane, sample)
        docs.append(doc)
    return docs


def flight_row(plane: Plane) -> dict[str, Any]:
    """The ``flights`` columns of a plane (JSON-safe: also queued in the
    episode outbox while the archive is down). ``status`` is the writer's."""
    track = plane.track
    return {
        "flight_id": plane.flight_id,
        "icao": plane.icao,
        "start_time": track.first_packet,
        "end_time": track.last_update,
        **info_text_fields(plane.info),
        "info": info_json(plane.info),
        "previous_flight_id": track.previous_flight_id,
    }


def integrity_rows(
    plane: Plane, flight_id: str, icao: str, *, after: float | None = None
) -> list[tuple[str, float, str, float | None, str, str, str | None]]:
    """``integrity`` table rows: one per logged verdict (newer than *after*).

    The log is append-only and each verdict is stamped when it was made, so
    a checkpoint writes only the verdicts after the last one it wrote.
    """
    return [
        (
            flight_id,
            verdict.time,
            icao,
            verdict.score,
            verdict.status,
            json_dumps(list(verdict.flagged)),
            json_dumps(verdict.checks) if verdict.checks is not None else None,
        )
        for verdict in plane.track.integrity_log
        if after is None or verdict.time > after
    ]


# --- per sample (live and archive) -----------------------------------------


def _add_sample_fields(doc: dict[str, Any], plane: Plane, sample: Sample) -> None:
    timestamp, _lat, _lon, alt, speed, heading = sample
    if alt is not None:
        doc["altitude"] = alt
    if speed is not None:
        doc["speed"] = speed
    if heading is not None:
        doc["heading"] = heading
    gnss_height = gnss_height_at(plane, timestamp)
    if gnss_height is not None:
        doc["gnss_height"] = gnss_height
    doc.update(sample_provenance(plane, timestamp))


def gnss_height_at(plane: Plane, timestamp: float) -> float | None:
    """GNSS height (m, WGS84 ellipsoid) in effect at a position sample, or None.

    TC 20-22 positions carry it instead of barometric altitude. The tracker
    stores a repeated value as the first and last datum of the run, so a
    sample between two equal datums is inside that run. Otherwise the latest
    height at or before the sample is used only while it is fresh, so baro
    positions after an aircraft stops sending GNSS ones get none.
    """
    series = plane.received_data.get(STORE_GNSS_HEIGHT) or []
    idx = bisect_right(series, timestamp, key=lambda datum: datum.time)
    if idx == 0:
        return None
    before = series[idx - 1]
    fresh = timestamp - before.time <= GNSS_HEIGHT_MAX_AGE
    in_run = idx < len(series) and series[idx].value == before.value
    if before.value is None or not (fresh or in_run):
        return None
    value = float(before.value)
    return value if math.isfinite(value) else None
