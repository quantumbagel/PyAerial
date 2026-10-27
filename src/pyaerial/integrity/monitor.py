"""Integrity monitor: gathers evidence from raw frames and scores each aircraft.

The engine hands every raw frame to :meth:`IntegrityMonitor.observe_frames`
before receiver merging and de-duplication (both hide evidence: copies from
several receivers are what TDOA needs, repeats are what replay detection
needs). Once a second, :meth:`IntegrityMonitor.evaluate` resolves new samples
against each aircraft's claimed track, calibrates the station-wide models
(signal strength vs. range, AoA bias, receiver clock offsets) on aircraft that
currently look genuine, runs the checks, and writes ``info["integrity"]``.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass

from pyModeS.util import altcode as pms_altcode, icao as pms_icao

from pyaerial.classify import (
    CAT_AIRBORNE_BARO,
    CAT_AIRBORNE_GNSS,
    CAT_IDENT,
    CAT_SURFACE,
    CAT_VELOCITY,
)
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_ALT,
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_PACKET_TYPE,
    STORE_RECV_DATA,
    STORE_VERT_SPEED,
)
from pyaerial.integrity import checks, geodesy
from pyaerial.integrity.checks import FAIL, WARN, CheckResult
from pyaerial.integrity.evidence import (
    AircraftEvidence,
    _series_value,
    latest_fix,
    position_at,
    series_window,
    trim,
)
from pyaerial.integrity.network import NetworkChecker
from pyaerial.integrity.stats import circular_median_deg
from pyaerial.integrity.tdoa import ClockSync, TdoaObservation, multilaterate
from pyaerial.models import nearest_datum
from pyaerial.receivers.frames import BEAST_MLAT_CLOCK, RawFrame
from pyaerial.receivers.positions import ReceiverPosition, ReceiverPositions
from pyaerial.tracker import STORE_GATE_REJECTS
from pyaerial.units import FT_TO_M

log = logging.getLogger("pyaerial.integrity")

STORE_INTEGRITY = "integrity"
STATUS_OK = "ok"
STATUS_SUSPECT = "suspect"
STATUS_SPOOFED = "spoofed"
STATUS_UNKNOWN = "unknown"

_EVAL_INTERVAL = 1.0
_RECENT = 30.0
_REPLAY_WINDOW = 5.0
_SATURATED_DBFS = -3.0
_TREND_MAX_ELEVATION = 30.0
_MIN_MODEL_AIRCRAFT = 8
_MIN_BIAS_AIRCRAFT = 5
_MIN_CORROBORATION_AIRCRAFT = 5
_CORROBORATION_RATE = 0.5
_ELIGIBLE_SECONDS = 60.0
_STARTUP_GRACE = 120.0
_FIRST_FIX_SAMPLES = 500
_MIN_FIRST_FIX_SAMPLES = 20
_CFO_CLUSTER_HZ = 100.0
_RSSI_CLUSTER_DB = 1.5
_BEARING_CLUSTER_DEG = 5.0
_REWRITE_SECONDS = 30.0
# Full per-aircraft checks run this often (staggered); evidence still
# accumulates every tick.
_CHECK_INTERVAL = 5.0
_MLAT_MAX_RMS_M = 500.0
_SHORT_DFS = frozenset({0, 4, 5, 11, 16, 20, 21})
# Checks that assume the aircraft itself is the transmitter.
_RF_CHECKS = frozenset(
    {
        "aoa",
        "tdoa",
        "radio_horizon",
        "rssi_range",
        "rssi_trend",
        "timing",
        "replay",
        "appearance",
        "message_mix",
        "mode_s_corroboration",
        "mode_s_altitude",
    }
)
_ALT_DFS = frozenset({0, 4, 16, 20})
_LEVEL = {FAIL: 1.0, WARN: 0.5}


def _is_airborne_position(hex_msg: str) -> bool:
    tc = int(hex_msg[8:10], 16) >> 3
    return 9 <= tc <= 18 or 20 <= tc <= 22


def _cpr_odd(hex_msg: str) -> int:
    # F flag: message bit 53 (ME bit 21) of a 112-bit airborne position.
    return (int(hex_msg, 16) >> (111 - 53)) & 1


def _option_float(options: dict[str, object], key: str) -> float | None:
    value = options.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


@dataclass(slots=True)
class _PlaneStats:
    """Per-aircraft summaries shared between calibration, checks and clustering."""

    rssi_index: dict[str, float]
    rssi_median: dict[str, float]
    bearing_median: dict[str, float]


class IntegrityMonitor:
    def __init__(
        self,
        config: Config,
        positions: ReceiverPositions,
        *,
        started_at: float | None = None,
    ):
        self.config = config
        self.cfg = config.integrity
        self.positions = positions
        self._evidence: dict[str, AircraftEvidence] = {}
        self._sync = ClockSync()
        self._known: set[str] = set()
        self._scores: dict[str, float] = {}
        self._statuses: dict[str, str] = {}
        self._signatures: dict[str, tuple] = {}
        self._written_at: dict[str, float] = {}
        self._first_fix: dict[str, tuple[float, float, str, float]] = {}
        self._first_fix_km: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=_FIRST_FIX_SAMPLES)
        )
        self._started = started_at if started_at is not None else time.time()
        self._last_eval = 0.0
        self._next_check: dict[str, float] = {}
        self._cached: dict[str, tuple[dict[str, CheckResult], dict[str, float] | None]] = {}
        self._stats: dict[str, _PlaneStats] = {}
        self._rssi_model: dict[str, float] = {}
        self._bearing_bias: dict[str, float] = {}
        self._corroboration_rate: float | None = None
        self._aoa_options: dict[str, tuple[float | None, float | None]] = {}
        for name, cfg in config.receivers.items():
            if cfg.type == "aoa":
                self._aoa_options[name] = (
                    _option_float(cfg.options, "bearing_offset"),
                    _option_float(cfg.options, "array_axis"),
                )
        self._network = (
            NetworkChecker(self.cfg.cross_network)
            if self.cfg.cross_network.enabled
            else None
        )

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._network is not None:
            self._network.start()

    def stop(self) -> None:
        if self._network is not None:
            self._network.stop()

    def forget(self, icao: str) -> None:
        key = icao.upper()
        for store in (
            self._evidence,
            self._scores,
            self._statuses,
            self._signatures,
            self._written_at,
            self._first_fix,
            self._next_check,
            self._cached,
            self._stats,
        ):
            store.pop(key, None)
        if self._network is not None:
            self._network.forget(key)

    # --- ingest -------------------------------------------------------------

    def _ev(self, icao: str, t: float) -> AircraftEvidence:
        ev = self._evidence.get(icao)
        if ev is None:
            ev = AircraftEvidence(last_seen=t)
            self._evidence[icao] = ev
        elif t > ev.last_seen:
            ev.last_seen = t
        return ev

    def observe_frames(self, frames: list[RawFrame]) -> None:
        """Record evidence from every raw frame (before merge and de-dup)."""
        copies: dict[str, dict[str, int]] = {}
        heard_at: dict[str, float] = {}
        for frame in frames:
            hex_msg = frame.hex
            if len(hex_msg) not in (14, 28):
                continue
            try:
                df = int(hex_msg[:2], 16) >> 3
            except ValueError:
                continue
            # MLAT results merged into the Beast stream are synthetic, not
            # radio observations.
            if frame.clock == BEAST_MLAT_CLOCK:
                continue
            t = frame.timestamp
            receiver = frame.receiver
            if df in (17, 18) and len(hex_msg) == 28:
                icao = hex_msg[2:8].upper()
                ev = self._ev(icao, t)
                if df == 18 and int(hex_msg[:2], 16) & 0x07 >= 2:
                    ev.rebroadcast = True
                    continue
                ev.rx_last[receiver] = t
                if frame.rssi is not None and math.isfinite(frame.rssi):
                    ev.rssi[receiver].append((t, frame.rssi))
                if frame.bearing is not None:
                    ev.aoa[receiver].append((t, frame.bearing, frame.elevation))
                if frame.cfo is not None:
                    ev.cfo.append((t, frame.cfo))
                clock = frame.clock
                if _is_airborne_position(hex_msg):
                    if clock is not None:
                        ev.pos_clock[receiver].append((clock, _cpr_odd(hex_msg)))
                    key = (receiver, hex_msg)
                    seen = ev.recent_pos.get(key)
                    if seen is not None and 0 < t - seen <= _REPLAY_WINDOW:
                        ev.replays.append((t,))
                    ev.recent_pos[key] = t
                if clock is not None and receiver:
                    copies.setdefault(hex_msg, {})[receiver] = clock
                    heard_at[hex_msg] = min(t, heard_at.get(hex_msg, t))
            elif df in _SHORT_DFS:
                try:
                    icao = pms_icao(hex_msg)
                except Exception:
                    continue
                # Short replies carry the address in the CRC, so a corrupted
                # frame yields a random one. Only addresses already tracked
                # from ADS-B count.
                if not icao or icao not in self._known:
                    continue
                ev = self._ev(icao, t)
                ev.mode_s.append((t, df))
                if df in _ALT_DFS:
                    try:
                        feet = pms_altcode(hex_msg)
                    except Exception:
                        feet = None
                    if feet is not None:
                        ev.mode_s_alt.append((t, feet * FT_TO_M))
        for hex_msg, clocks in copies.items():
            if len(clocks) >= 2:
                self._evidence[hex_msg[2:8].upper()].add_tdoa(
                    TdoaObservation(heard_at[hex_msg], clocks)
                )

    # --- evaluation ---------------------------------------------------------

    def evaluate(
        self, planes: dict[str, dict], now: float | None = None, *, force: bool = False
    ) -> None:
        """Score *planes*. *force* skips the 1 s throttle and re-checks every aircraft."""
        if not self.cfg.enabled:
            return
        now = time.time() if now is None else now
        if not force and now - self._last_eval < _EVAL_INTERVAL:
            return
        self._last_eval = now
        self._known = set(planes)
        cutoff = now - self.cfg.window_seconds
        antennas = self.positions.all()
        ecefs = {
            name: geodesy.ecef(p.latitude, p.longitude, p.altitude)
            for name, p in antennas.items()
        }

        for icao, ev in list(self._evidence.items()):
            if icao not in planes and ev.last_seen < cutoff:
                del self._evidence[icao]
                continue
            self._trim(ev, now, cutoff)
        for icao in [icao for icao in self._cached if icao not in planes]:
            self.forget(icao)

        for icao, plane in planes.items():
            ev = self._evidence.get(icao)
            if ev is not None:
                self._resolve(ev, plane, antennas)
            self._note_first_fix(icao, plane, ev)

        self._sync_clocks(planes, ecefs)
        self._sync.refit(now)
        for icao, plane in planes.items():
            ev = self._evidence.get(icao)
            if ev is not None:
                self._tdoa_residuals(ev, plane, ecefs)
        due: set[str] = set()
        for icao in planes:
            next_check = self._next_check.get(icao)
            if next_check is None:
                # Spread aircraft across the interval so no tick does them all.
                spread = zlib.crc32(icao.encode()) % 1000 / 1000
                self._next_check[icao] = now + spread * _CHECK_INTERVAL
            elif now < next_check and icao in self._cached and not force:
                continue
            else:
                self._next_check[icao] = now + _CHECK_INTERVAL
            due.add(icao)
            self._stats[icao] = self._plane_stats(icao, now)
        stats = {icao: self._stats[icao] for icao in planes}
        self._calibrate(planes, stats, now)

        results: dict[str, dict[str, CheckResult]] = {}
        emitters: dict[str, dict[str, float]] = {}
        for icao, plane in planes.items():
            if icao in due:
                ev = self._evidence.get(icao)
                self._cached[icao] = self._check_plane(
                    icao, plane, ev, stats[icao], antennas, ecefs, now
                )
            found, emitter = self._cached[icao]
            results[icao] = dict(found)
            if emitter is not None:
                emitters[icao] = emitter
        self._clusters(results, stats, now)

        for icao, plane in planes.items():
            self._publish(icao, plane, results[icao], emitters.get(icao), now)

    def _trim(self, ev: AircraftEvidence, now: float, cutoff: float) -> None:
        for group in (ev.rssi, ev.rssi_geo, ev.aoa, ev.aoa_geo):
            for series in group.values():
                trim(series, cutoff)
        for series in (ev.cfo, ev.replays, ev.mode_s, ev.mode_s_alt, ev.tdoa_resid):
            trim(series, cutoff)
        while ev.tdoa and ev.tdoa[0].time < cutoff:
            ev.tdoa.popleft()
        stale = now - _REPLAY_WINDOW
        if ev.recent_pos:
            ev.recent_pos = {k: t for k, t in ev.recent_pos.items() if t >= stale}

    def _antenna(self, antennas: dict[str, ReceiverPosition], name: str) -> ReceiverPosition:
        return antennas.get(name) or self.positions.get(name)

    def _resolve(
        self,
        ev: AircraftEvidence,
        plane: dict,
        antennas: dict[str, ReceiverPosition],
    ) -> None:
        """Turn new raw RSSI / AoA samples into range and angle samples."""
        for receiver, raw in ev.rssi.items():
            cursor = ev.rssi_cursor.get(receiver, 0.0)
            fresh = _newer_than(raw, cursor)
            if not fresh:
                continue
            ant = self._antenna(antennas, receiver)
            origin = geodesy.ecef(ant.latitude, ant.longitude, ant.altitude)
            for t, rssi in fresh:
                pos = position_at(plane, t)
                if pos is None:
                    continue
                alt = pos[2] if pos[2] is not None else ant.altitude
                target = geodesy.ecef(pos[0], pos[1], alt)
                slant_km = max(geodesy.slant_range_m(origin, target) / 1000.0, 0.01)
                elev = geodesy.elevation_deg(
                    ant.latitude, ant.longitude, ant.altitude, pos[0], pos[1], alt
                )
                ev.rssi_geo[receiver].append((t, rssi, slant_km, elev))
            ev.rssi_cursor[receiver] = fresh[-1][0]
        for receiver, raw in ev.aoa.items():
            cursor = ev.aoa_cursor.get(receiver, 0.0)
            fresh = _newer_than(raw, cursor)
            if not fresh:
                continue
            ant = self._antenna(antennas, receiver)
            for t, bearing, elevation in fresh:
                pos = position_at(plane, t)
                if pos is None:
                    continue
                alt = pos[2] if pos[2] is not None else ant.altitude
                expected = geodesy.initial_bearing_deg(
                    ant.latitude, ant.longitude, pos[0], pos[1]
                )
                expected_elev = geodesy.elevation_deg(
                    ant.latitude, ant.longitude, ant.altitude, pos[0], pos[1], alt
                )
                ev.aoa_geo[receiver].append((t, bearing, expected, elevation, expected_elev))
            ev.aoa_cursor[receiver] = fresh[-1][0]

    def _note_first_fix(self, icao: str, plane: dict, ev: AircraftEvidence | None) -> None:
        if icao in self._first_fix or ev is None:
            return
        recv = plane.get(STORE_RECV_DATA) or {}
        lats = recv.get(STORE_LAT) or []
        lons = recv.get(STORE_LONG) or []
        if not lats or not lons:
            return
        t0 = lats[0].time
        alt = _series_value(recv.get(STORE_ALT) or [], t0, 5.0)
        best: tuple[float, str] | None = None
        for receiver in ev.rx_last:
            ant = self.positions.get(receiver)
            km = geodesy.ground_distance_km(ant.latitude, ant.longitude, lats[0].value, lons[0].value)
            if best is None or km < best[0]:
                best = (km, receiver)
        if best is None:
            return
        km, receiver = best
        self._first_fix[icao] = (km, alt if alt is not None else math.nan, receiver, t0)
        if t0 - self._started >= _STARTUP_GRACE:
            self._first_fix_km[receiver].append(km)

    def _sync_clocks(self, planes: dict[str, dict], ecefs: dict[str, geodesy.Vec3]) -> None:
        """Feed frames from aircraft that currently look genuine into clock sync."""
        if len(ecefs) < 2:
            return
        for icao, plane in planes.items():
            ev = self._evidence.get(icao)
            if ev is None or not ev.tdoa:
                continue
            trusted = self._scores.get(icao, 1.0) >= self.cfg.ok_score
            newest = ev.tdoa_synced_until
            for obs in ev.tdoa:
                if obs.time <= ev.tdoa_synced_until:
                    continue
                newest = max(newest, obs.time)
                if not trusted:
                    continue
                pos = position_at(plane, obs.time, max_gap=1.0)
                if pos is None or pos[2] is None:
                    continue
                self._sync.add_sync(obs, pos, ecefs, icao)
            ev.tdoa_synced_until = newest

    def _tdoa_residuals(
        self, ev: AircraftEvidence, plane: dict, ecefs: dict[str, geodesy.Vec3]
    ) -> None:
        """Test each new multi-receiver observation once against the claim."""
        if not ev.tdoa or len(ecefs) < 2:
            return
        max_spread = self.cfg.tdoa_tolerance_m / 2
        newest = ev.tdoa_cursor
        for obs in reversed(ev.tdoa):
            if obs.time <= ev.tdoa_cursor:
                break
            newest = max(newest, obs.time)
            pos = position_at(plane, obs.time, max_gap=1.0)
            if pos is None or pos[2] is None:
                continue
            for residual in self._sync.residuals_m(obs, pos, ecefs, max_spread):
                ev.tdoa_resid.append((obs.time, residual))
        ev.tdoa_cursor = newest

    def _plane_stats(self, icao: str, now: float) -> _PlaneStats:
        ev = self._evidence.get(icao)
        stats = _PlaneStats({}, {}, {})
        if ev is None:
            return stats
        recent = now - _RECENT
        for receiver, rows in ev.rssi_geo.items():
            usable = [r for r in rows if r[0] >= recent and r[1] < _SATURATED_DBFS]
            if len(usable) < 3:
                continue
            stats.rssi_index[receiver] = statistics.median(
                rssi + 20 * math.log10(slant) for _t, rssi, slant, _e in usable
            )
            stats.rssi_median[receiver] = statistics.median(r[1] for r in usable)
        for receiver, rows in ev.aoa_geo.items():
            corrected = [
                self._corrected_bearing(receiver, measured)
                for t, measured, _exp, _el, _eel in rows
                if t >= recent
            ]
            if len(corrected) >= 5:
                median = circular_median_deg(corrected)
                if median is not None:
                    stats.bearing_median[receiver] = median
        return stats

    def _calibrate(self, planes: dict[str, dict], stats: dict[str, _PlaneStats], now: float) -> None:
        trusted = [
            icao for icao in planes if self._scores.get(icao, 1.0) >= self.cfg.ok_score
        ]
        # Station signal model: rssi + 20 log10(range) is constant for one
        # transmitter power; its median over genuine aircraft is the station's.
        by_receiver: dict[str, list[float]] = defaultdict(list)
        for icao in trusted:
            for receiver, index in stats[icao].rssi_index.items():
                by_receiver[receiver].append(index)
        self._rssi_model = {
            receiver: statistics.median(values)
            for receiver, values in by_receiver.items()
            if len(values) >= _MIN_MODEL_AIRCRAFT
        }

        # AoA bias (array orientation) for receivers without a configured offset.
        recent = now - _RECENT
        for receiver, (offset, axis) in self._aoa_options.items():
            if offset is not None or axis is not None:
                continue
            per_plane: list[float] = []
            for icao in trusted:
                ev = self._evidence.get(icao)
                rows = ev.aoa_geo.get(receiver) if ev is not None else None
                if not rows:
                    continue
                diffs = [
                    geodesy.angle_diff_deg(measured, expected)
                    for t, measured, expected, _el, _eel in rows
                    if t >= recent
                ]
                if len(diffs) >= 5:
                    per_plane.append(statistics.median(diffs))
            if len(per_plane) >= _MIN_BIAS_AIRCRAFT:
                bias = circular_median_deg(per_plane)
                if bias is not None:
                    self._bearing_bias[receiver] = geodesy.angle_diff_deg(bias, 0.0)

        # How often aircraft here are also seen replying to SSR interrogation.
        eligible = 0
        corroborated = 0
        for icao, plane in planes.items():
            if not self._corroboration_eligible(plane, now):
                continue
            eligible += 1
            ev = self._evidence.get(icao)
            corroborated += bool(ev is not None and ev.mode_s)
        self._corroboration_rate = (
            corroborated / eligible if eligible >= _MIN_CORROBORATION_AIRCRAFT else None
        )

    def _corroboration_eligible(self, plane: dict, now: float) -> bool:
        internal = plane.get(STORE_INTERNAL) or {}
        first = internal.get(STORE_FIRST_PACKET)
        if first is None or now - first < _ELIGIBLE_SECONDS:
            return False
        pkt = internal.get(STORE_PACKET_TYPE) or {}
        return pkt.get(CAT_AIRBORNE_BARO, 0) + pkt.get(CAT_AIRBORNE_GNSS, 0) >= 20

    def _corrected_bearing(self, receiver: str, measured: float) -> float:
        offset, _axis = self._aoa_options.get(receiver, (None, None))
        if offset is not None:
            return (measured + offset) % 360.0
        return (measured - self._bearing_bias.get(receiver, 0.0)) % 360.0

    def _bearing_error(self, receiver: str, measured: float, expected: float) -> float:
        corrected = self._corrected_bearing(receiver, measured)
        error = geodesy.angle_diff_deg(corrected, expected)
        _offset, axis = self._aoa_options.get(receiver, (None, None))
        if axis is not None:
            mirrored = (2 * axis - corrected) % 360.0
            other = geodesy.angle_diff_deg(mirrored, expected)
            if abs(other) < abs(error):
                error = other
        return error

    # --- per-aircraft checks ------------------------------------------------

    def _check_plane(
        self,
        icao: str,
        plane: dict,
        ev: AircraftEvidence | None,
        stats: _PlaneStats,
        antennas: dict[str, ReceiverPosition],
        ecefs: dict[str, geodesy.Vec3],
        now: float,
    ) -> tuple[dict[str, CheckResult], dict[str, float] | None]:
        cfg = self.cfg
        out: dict[str, CheckResult | None] = {}
        emitter: dict[str, float] | None = None
        info = plane.get(STORE_INFO) or {}
        internal = plane.get(STORE_INTERNAL) or {}
        recv = plane.get(STORE_RECV_DATA) or {}
        pkt = internal.get(STORE_PACKET_TYPE) or {}
        first_packet = internal.get(STORE_FIRST_PACKET, now)
        fix = latest_fix(plane)
        recent = now - _RECENT

        out["identity"] = checks.identity(icao, info.get(STORE_CALLSIGN), info.get("registration"))
        positions = pkt.get(CAT_AIRBORNE_BARO, 0) + pkt.get(CAT_AIRBORNE_GNSS, 0)
        out["duplicate_icao"] = checks.duplicate_icao(
            int(internal.get(STORE_GATE_REJECTS, 0)),
            positions + pkt.get(CAT_SURFACE, 0),
        )
        if now - first_packet >= _ELIGIBLE_SECONDS and pkt.get(CAT_SURFACE, 0) == 0:
            out["message_mix"] = checks.message_mix(
                positions, pkt.get(CAT_IDENT, 0), pkt.get(CAT_VELOCITY, 0)
            )
        out["kinematics"] = self._kinematics(plane, fix)

        if ev is not None:
            if fix is not None and fix[2] is not None:
                distances = []
                for receiver, heard in ev.rx_last.items():
                    if heard < recent:
                        continue
                    ant = self._antenna(antennas, receiver)
                    km = geodesy.ground_distance_km(ant.latitude, ant.longitude, fix[0], fix[1])
                    limit = geodesy.radio_horizon_km(
                        max(ant.antenna_height, ant.altitude), fix[2]
                    ) * cfg.horizon_margin
                    distances.append((receiver, km, limit))
                out["radio_horizon"] = checks.radio_horizon(distances, cfg.max_range_km)

            excess = [
                (receiver, index - self._rssi_model[receiver])
                for receiver, index in stats.rssi_index.items()
                if receiver in self._rssi_model
            ]
            out["rssi_range"] = checks.rssi_range(excess, cfg.rssi_excess_db)
            out["rssi_trend"] = self._rssi_trend(ev)
            out["aoa"] = self._aoa(ev, recent)
            tdoa_result, emitter = self._tdoa(plane, ev, fix, ecefs, recent)
            out["tdoa"] = tdoa_result
            out["mode_s_altitude"] = self._mode_s_altitude(ev, recv, pkt)
            if self._corroboration_rate is not None and self._corroboration_eligible(plane, now):
                if self._corroboration_rate >= _CORROBORATION_RATE:
                    out["mode_s_corroboration"] = checks.mode_s_corroboration(
                        bool(ev.mode_s), self._corroboration_rate
                    )
            out["timing"] = self._timing(ev)
            speed = nearest_datum(recv.get(STORE_HORIZ_SPEED) or [], now)
            out["replay"] = checks.replay(
                len(ev.replays), speed is not None and speed.value > 50
            )

        first = self._first_fix.get(icao)
        if first is not None:
            km, alt, receiver, t0 = first
            typical = self._first_fix_km.get(receiver)
            if (
                t0 - self._started >= _STARTUP_GRACE
                and math.isfinite(alt)
                and typical is not None
                and len(typical) >= _MIN_FIRST_FIX_SAMPLES
            ):
                out["appearance"] = checks.appearance(km, alt, statistics.median(typical))

        if self._network is not None and fix is not None and now - first_packet >= _RECENT:
            self._network.request(icao, now)
            sighting = self._network.result(icao)
            if sighting is not None:
                distance = None
                if sighting.found and sighting.latitude is not None and sighting.longitude is not None:
                    here = position_at(plane, sighting.position_time or now, max_gap=10.0)
                    lat, lon = (here[0], here[1]) if here else (fix[0], fix[1])
                    distance = geodesy.ground_distance_km(
                        lat, lon, sighting.latitude, sighting.longitude
                    )
                long_tracked = (
                    now - first_packet >= 120 and fix[2] is not None and fix[2] > 3000
                )
                out["cross_network"] = checks.cross_network(
                    distance,
                    sighting.found,
                    cfg.cross_network.max_distance_km,
                    long_tracked,
                )

        if ev is not None and ev.rebroadcast:
            out = {name: r for name, r in out.items() if name not in _RF_CHECKS}
            emitter = None
        return {name: result for name, result in out.items() if result is not None}, emitter

    def _kinematics(
        self, plane: dict, fix: tuple[float, float, float | None, float] | None
    ) -> CheckResult | None:
        if fix is None:
            return None
        recv = plane.get(STORE_RECV_DATA) or {}
        lat_series = recv.get(STORE_LAT) or []
        lon_series = recv.get(STORE_LONG) or []
        t1 = fix[3]
        target = t1 - 15.0
        lat0 = nearest_datum(lat_series, target)
        lon0 = nearest_datum(lon_series, target)
        if lat0 is None or lon0 is None:
            return None
        t0 = lat0.time if abs(lat0.time - target) <= abs(lon0.time - target) else lon0.time
        elapsed = t1 - t0
        if not 8.0 <= elapsed <= 30.0:
            return None
        distance_m = geodesy.ground_distance_km(lat0.value, lon0.value, fix[0], fix[1]) * 1000
        bearing = (
            geodesy.initial_bearing_deg(lat0.value, lon0.value, fix[0], fix[1])
            if distance_m > 0
            else None
        )
        speeds = [d.value for d in series_window(recv.get(STORE_HORIZ_SPEED) or [], t0, t1)]
        tracks = [d.value for d in series_window(recv.get(STORE_HEADING) or [], t0, t1)]
        vrates = [d.value for d in series_window(recv.get(STORE_VERT_SPEED) or [], t0, t1)]
        alt_series = recv.get(STORE_ALT) or []
        alt0 = _series_value(alt_series, t0, 3.0)
        alt1 = _series_value(alt_series, t1, 3.0)
        climb = (alt1 - alt0) / elapsed if alt0 is not None and alt1 is not None else None
        return checks.kinematics(
            elapsed_s=elapsed,
            distance_m=distance_m,
            displacement_bearing=bearing,
            reported_speed_kph=statistics.median(speeds) if speeds else None,
            reported_tracks=tracks,
            climb_mps=climb,
            reported_vrate_mps=statistics.median(vrates) if vrates else None,
        )

    def _rssi_trend(self, ev: AircraftEvidence) -> CheckResult | None:
        best: CheckResult | None = None
        for rows in ev.rssi_geo.values():
            samples = [
                (20 * math.log10(slant), rssi)
                for _t, rssi, slant, elev in rows
                if rssi < _SATURATED_DBFS and elev < _TREND_MAX_ELEVATION
            ]
            best = checks.worst([best, checks.rssi_trend(samples)])
        return best

    def _aoa(self, ev: AircraftEvidence, recent: float) -> CheckResult | None:
        found: list[CheckResult | None] = []
        for receiver, rows in ev.aoa_geo.items():
            bearing_errors: list[float] = []
            elevation_errors: list[float] = []
            for t, measured, expected, elev, expected_elev in rows:
                if t < recent:
                    continue
                bearing_errors.append(self._bearing_error(receiver, measured, expected))
                if elev is not None:
                    elevation_errors.append(elev - expected_elev)
            found.append(
                checks.aoa(bearing_errors, elevation_errors, self.cfg.aoa_tolerance_deg, receiver)
            )
        return checks.worst(found)

    def _tdoa(
        self,
        plane: dict,
        ev: AircraftEvidence,
        fix: tuple[float, float, float | None, float] | None,
        ecefs: dict[str, geodesy.Vec3],
        recent: float,
    ) -> tuple[CheckResult | None, dict[str, float] | None]:
        if not ev.tdoa or len(ecefs) < 2:
            return None, None
        tolerance = self.cfg.tdoa_tolerance_m
        max_spread = tolerance / 2
        residuals = [r for t, r in ev.tdoa_resid if t >= recent]
        result = checks.tdoa(residuals, tolerance)
        # Locate the real transmitter only for a target that already fails:
        # far outside the receiver baseline the geometry is too weak for the
        # solution itself to be evidence.
        if result is None or result.status != FAIL or fix is None:
            return result, None
        ground = statistics.median(geodesy.geodetic(*xyz)[2] for xyz in ecefs.values())
        solutions: list[tuple[float, float]] = []
        multi = [obs for obs in ev.tdoa if obs.time >= recent and len(obs.clocks) >= 3]
        for obs in multi[-5:]:
            diffs = self._sync.range_differences(obs, ecefs, max_spread)
            if diffs is None:
                continue
            ref, others = diffs
            claimed_alt = fix[2] if fix[2] is not None else ground
            # Two differences fit any height exactly, so assume ground level,
            # where a spoofer usually is; with more, the claimed altitude
            # competes on residual.
            heights = [claimed_alt, ground] if len(others) >= 3 else [ground]
            best = None
            for height in heights:
                solved = multilaterate(
                    ecefs[ref],
                    [(ecefs[name], diff) for name, diff in others],
                    geodesy.ecef(fix[0], fix[1], height),
                    fixed_altitude=height,
                )
                if solved is not None and (best is None or solved[1] < best[1]):
                    best = solved
            if best is None or best[1] > _MLAT_MAX_RMS_M:
                continue
            lat, lon, _alt = geodesy.geodetic(*best[0])
            solutions.append((lat, lon))
        if not solutions:
            return result, None
        lat = statistics.median(s[0] for s in solutions)
        lon = statistics.median(s[1] for s in solutions)
        emitter = {
            "latitude": round(lat, 5),
            "longitude": round(lon, 5),
            "offset_km": round(geodesy.ground_distance_km(lat, lon, fix[0], fix[1]), 1),
        }
        return result, emitter

    def _mode_s_altitude(self, ev: AircraftEvidence, recv: dict, pkt: dict) -> CheckResult | None:
        # Mode S replies carry barometric altitude; GNSS-height positions
        # would differ by the geoid/pressure offset, not by spoofing.
        if pkt.get(CAT_AIRBORNE_GNSS, 0) > pkt.get(CAT_AIRBORNE_BARO, 0):
            return None
        series = recv.get(STORE_ALT) or []
        diffs = []
        for t, alt in ev.mode_s_alt:
            adsb = _series_value(series, t, 3.0)
            if adsb is not None:
                diffs.append(alt - adsb)
        return checks.mode_s_altitude(diffs, self.cfg.mode_s_altitude_tolerance_m)

    def _timing(self, ev: AircraftEvidence) -> CheckResult | None:
        if not ev.pos_clock:
            return None
        rows = max(ev.pos_clock.values(), key=len)
        return checks.timing(list(rows))

    # --- cross-aircraft -----------------------------------------------------

    def _clusters(
        self,
        results: dict[str, dict[str, CheckResult]],
        stats: dict[str, _PlaneStats],
        now: float,
    ) -> None:
        """Flag groups of aircraft that share one transmitter's fingerprint."""
        recent = now - 60.0
        cfo: list[tuple[float, str]] = []
        for icao in results:
            ev = self._evidence.get(icao)
            if ev is None:
                continue
            values = [hz for t, hz in ev.cfo if t >= recent]
            if len(values) >= 3:
                cfo.append((statistics.median(values), icao))
        for group in _groups(cfo, _CFO_CLUSTER_HZ):
            centre = statistics.median(v for v, _ in group)
            for _v, icao in group:
                results[icao]["emitter_cluster"] = checks.emitter_cluster(
                    f"carrier offset {centre:+.0f} Hz", len(group)
                )

        loud: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for icao, stat in stats.items():
            for receiver, index in stat.rssi_index.items():
                model = self._rssi_model.get(receiver)
                if model is not None and index - model > self.cfg.rssi_excess_db / 2:
                    loud[receiver].append((stat.rssi_median[receiver], icao))
        for receiver, rows in loud.items():
            for group in _groups(rows, _RSSI_CLUSTER_DB):
                for _v, icao in group:
                    results[icao].setdefault(
                        "emitter_cluster",
                        checks.emitter_cluster(f"{receiver} signal level", len(group)),
                    )

        bearings: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for icao, found in results.items():
            aoa = found.get("aoa")
            if aoa is None or aoa.status != FAIL:
                continue
            for receiver, bearing in stats[icao].bearing_median.items():
                bearings[receiver].append((bearing, icao))
        for receiver, rows in bearings.items():
            for group in _groups(rows, _BEARING_CLUSTER_DEG, min_size=2):
                centre = circular_median_deg([v for v, _ in group]) or 0.0
                for _v, icao in group:
                    results[icao].setdefault(
                        "emitter_cluster",
                        checks.emitter_cluster(f"{receiver} bearing {centre:.0f}°", len(group)),
                    )

    # --- output -------------------------------------------------------------

    def _publish(
        self,
        icao: str,
        plane: dict,
        found: dict[str, CheckResult],
        emitter: dict[str, float] | None,
        now: float,
    ) -> None:
        score = 1.0
        weighted: dict[str, CheckResult] = {}
        for name, result in found.items():
            weight = self.cfg.weight(name)
            if weight <= 0:
                continue
            weighted[name] = result
            score *= 1.0 - weight * _LEVEL.get(result.status, 0.0)
        if not weighted:
            status = STATUS_UNKNOWN
        elif score >= self.cfg.ok_score:
            status = STATUS_OK
        elif score < self.cfg.spoofed_score:
            status = STATUS_SPOOFED
        else:
            status = STATUS_SUSPECT
        score = round(score, 2)
        self._scores[icao] = score

        previous = self._statuses.get(icao)
        self._statuses[icao] = status
        if status == STATUS_SPOOFED and previous != STATUS_SPOOFED:
            info = plane.get(STORE_INFO) or {}
            reasons = "; ".join(
                f"{name}: {r.detail}" for name, r in weighted.items() if r.status == FAIL
            )
            log.warning(
                "Integrity: %s %s looks spoofed (score %.2f): %s",
                icao,
                info.get(STORE_CALLSIGN) or "",
                score,
                reasons,
            )

        signature = (
            status,
            score,
            tuple(sorted((name, r.status) for name, r in weighted.items())),
            emitter is not None,
            icao in self._evidence and self._evidence[icao].rebroadcast,
        )
        if (
            signature == self._signatures.get(icao)
            and now - self._written_at.get(icao, 0.0) < _REWRITE_SECONDS
        ):
            return
        self._signatures[icao] = signature
        self._written_at[icao] = now
        doc: dict[str, object] = {
            "score": score,
            "status": status,
            "checks": {name: r.as_dict() for name, r in sorted(weighted.items())},
        }
        if emitter is not None:
            doc["emitter"] = emitter
        ev = self._evidence.get(icao)
        if ev is not None and ev.rebroadcast:
            doc["rebroadcast"] = True
        plane.setdefault(STORE_INFO, {})[STORE_INTEGRITY] = doc

    def score(self, icao: str) -> float | None:
        return self._scores.get(icao.upper())


def _newer_than(series: deque, cursor: float) -> list:
    """Samples with time > *cursor*, oldest first (walks back from the end)."""
    fresh = []
    for row in reversed(series):
        if row[0] <= cursor:
            break
        fresh.append(row)
    fresh.reverse()
    return fresh


def _groups(
    rows: list[tuple[float, str]], tolerance: float, *, min_size: int = 3
) -> list[list[tuple[float, str]]]:
    """Chains of sorted values whose neighbours are within *tolerance*."""
    if len(rows) < min_size:
        return []
    ordered = sorted(rows)
    groups: list[list[tuple[float, str]]] = []
    current = [ordered[0]]
    for row in ordered[1:]:
        if row[0] - current[-1][0] <= tolerance:
            current.append(row)
        else:
            if len(current) >= min_size:
                groups.append(current)
            current = [row]
    if len(current) >= min_size:
        groups.append(current)
    return groups
