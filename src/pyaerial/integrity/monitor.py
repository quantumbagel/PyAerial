"""Integrity monitor: gathers evidence from raw frames and scores each aircraft.

The engine hands every raw frame to :meth:`IntegrityMonitor.observe_frames`
before receiver merging and de-duplication (both hide evidence: copies from
several receivers are what TDOA needs, repeats are what replay detection
needs). Once a second, :meth:`IntegrityMonitor.evaluate` resolves new samples
against each aircraft's claimed track, calibrates the station-wide models
(signal strength vs. range, AoA bias, receiver clock offsets) on aircraft that
currently look genuine, runs the checks, and writes ``plane.info.integrity``.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import cast

from pyModeS.util import altcode as pms_altcode
from pyModeS.util import icao as pms_icao

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
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
    STORE_VERT_SPEED,
)
from pyaerial.integrity import checks, geodesy
from pyaerial.integrity.checks import FAIL, PASS, WARN, CheckResult
from pyaerial.integrity.evidence import (
    GEO_SPACING,
    TIMING_MAX_SAMPLES,
    AircraftEvidence,
    PositionLookup,
    _series_value,
    latest_fix,
    position_at,
    rows_since,
    series_window,
    trim,
)
from pyaerial.integrity.network import NetworkChecker
from pyaerial.integrity.stats import circular_median_deg, fast_median
from pyaerial.integrity.tdoa import (
    CLOCK_JUMP_SECONDS,
    ClockSync,
    TdoaObservation,
    clock_jump_seconds,
    multilaterate,
    wrap_ticks,
)
from pyaerial.models import IntegrityDoc, Plane, TrackState, Verdict, nearest_datum
from pyaerial.receivers.frames import (
    BEAST_CLOCK_HZ,
    BEAST_CLOCK_MOD,
    BEAST_MLAT_CLOCK,
    RawFrame,
    es_icao,
)
from pyaerial.receivers.positions import ReceiverPosition, ReceiverPositions
from pyaerial.units import FT_TO_M

log = logging.getLogger("pyaerial.integrity")

STATUS_OK = "ok"
STATUS_SUSPECT = "suspect"
STATUS_SPOOFED = "spoofed"
STATUS_UNKNOWN = "unknown"

# A claimed (lat, lon, alt_m) position whose altitude is known.
_Fix3D = tuple[float, float, float]

# Each aircraft is re-evaluated this often, at a phase set by its address,
# so every tick handles an even share; station-wide work runs this often too.
_EVAL_INTERVAL = 1.0
_HALF_MOD = BEAST_CLOCK_MOD // 2
_FAILING = frozenset({"suspect", "spoofed"})
_RECENT = 30.0
_REPLAY_WINDOW = 5.0
# Copies of one hex received this close together are one transmission
# (identical squitters repeat at least 0.4 s apart).
_COPY_WINDOW = 0.2
_SATURATED_DBFS = -3.0
_TREND_MAX_ELEVATION = 30.0
_MIN_MODEL_AIRCRAFT = 8
_MIN_BIAS_AIRCRAFT = 5
# Linear arrays: aircraft whose direct or mirrored bias lies this close agree.
_BIAS_AGREE_DEG = 10.0
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
_CHECK_SLOTS = round(_CHECK_INTERVAL / _EVAL_INTERVAL)
_MLAT_MAX_RMS_M = 500.0
# Multi-receiver frames kept after clock sync and residuals have used them.
_TDOA_KEEP = 32
# Caps are this many times the decimated sample count of the window, so the
# time trim binds; they only stop a flood (or clock skew) growing memory.
_CAP_HEADROOM = 1.5
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
# A passing score reads as ``ok`` (and the aircraft calibrates the station
# models) only once it has been tracked this long with this many checks
# reporting; until then it publishes ``unknown``. Failures publish at once.
_TRUST_MIN_SECONDS = 30.0
_TRUST_MIN_CHECKS = 3
# TIS-B / ADS-R targets get no RF checks (and never calibrate anything).
_TRUST_MIN_CHECKS_REBROADCAST = 2
# A status improves only after the better status has held this long; it
# worsens at once. Stops a score near a threshold flapping the verdict.
_RECOVER_SECONDS = 30.0
_SEVERITY = {STATUS_UNKNOWN: 0, STATUS_OK: 0, STATUS_SUSPECT: 1, STATUS_SPOOFED: 2}


def _track_started(track: TrackState) -> float:
    """When tracking of this aircraft began.

    The engine splits long flights and moves ``first_packet`` to the split;
    ``track_started`` survives it, so checks that wait for track age (message
    mix, cross-network, Mode S corroboration, the trust bootstrap) carry on.
    """
    started = track.track_started
    return track.first_packet if started is None else started


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
    # Receivers that only heard it saturated: rssi_index is at least this.
    rssi_floor: dict[str, float]


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
        window = self.cfg.window_seconds
        receivers = len(config.receivers)
        pairs = max(1, receivers * (receivers - 1) // 2)
        self._geo_maxlen = int(window / GEO_SPACING * _CAP_HEADROOM) + 16
        # Residuals are judged over the last _RECENT seconds only.
        # Position squitters: 2/s per receiver.
        self._timing_maxlen = min(
            TIMING_MAX_SAMPLES, int(window * 2 * _CAP_HEADROOM) + 16
        )
        self._resid_maxlen = (
            int(min(window, _RECENT) / GEO_SPACING * pairs * _CAP_HEADROOM) + 16
        )
        self._sync = ClockSync()
        # receiver -> (wall time, clock) of its newest frame with a clock
        self._rx_clock: dict[str, tuple[float, int]] = {}
        # receiver -> wall time of its last clock discontinuity
        self._clock_jumps: dict[str, float] = {}
        self._known: set[str] = set()
        self._scores: dict[str, float] = {}
        # Score without the aoa check: AoA bias is learned from aircraft that
        # pass the other checks, or a wrong bias would never be corrected.
        self._aoa_trust: dict[str, float] = {}
        self._statuses: dict[str, str] = {}
        # Aircraft with enough evidence to be called ok (sticky).
        self._established: set[str] = set()
        # Last time the raw status was at least as bad as the published one.
        self._held_at: dict[str, float] = {}
        self._signatures: dict[str, tuple] = {}
        # (status, signature, time) of the last verdict appended to the log.
        self._logged: dict[str, tuple[str, tuple, float]] = {}
        self._written_at: dict[str, float] = {}
        self._first_fix: dict[str, tuple[float, float, str, float]] = {}
        self._first_fix_km: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=_FIRST_FIX_SAMPLES)
        )
        self._started = started_at if started_at is not None else time.time()
        self._last_eval = 0.0
        # icao -> evaluation phase (s) within _EVAL_INTERVAL, and last slot run
        self._phase: dict[str, float] = {}
        self._eval_slot: dict[str, int] = {}
        # icao -> emitter_cluster result from the last station-wide pass
        self._cluster_flags: dict[str, CheckResult] = {}
        self._cached: dict[
            str, tuple[dict[str, CheckResult], dict[str, float] | None]
        ] = {}
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
            if self.cfg.enabled and self.cfg.cross_network.enabled
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
            self._aoa_trust,
            self._statuses,
            self._held_at,
            self._signatures,
            self._logged,
            self._written_at,
            self._first_fix,
            self._phase,
            self._eval_slot,
            self._cluster_flags,
            self._cached,
            self._stats,
        ):
            store.pop(key, None)
        self._established.discard(key)
        if self._network is not None:
            self._network.forget(key)

    # --- ingest -------------------------------------------------------------

    def _ev(self, icao: str, t: float) -> AircraftEvidence:
        ev = self._evidence.get(icao)
        if ev is None:
            ev = AircraftEvidence(
                last_seen=t,
                geo_maxlen=self._geo_maxlen,
                resid_maxlen=self._resid_maxlen,
                timing_maxlen=self._timing_maxlen,
            )
            self._evidence[icao] = ev
        elif t > ev.last_seen:
            ev.last_seen = t
        return ev

    def observe_frames(self, frames: list[RawFrame]) -> None:
        """Record evidence from every raw frame (before merge and de-dup)."""
        if not self.cfg.enabled:
            # evaluate() would never trim it.
            return
        # hex -> [(wall time, receiver, clock)] of every copy with a clock
        copies: dict[str, list[tuple[float, str, int]]] = {}
        # hex -> address (None: bad parity or not an ICAO address); every
        # receiver delivers its own copy, so check each frame once.
        addresses: dict[str, str | None] = {}
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
            if frame.clock is not None and receiver:
                self._watch_clock(receiver, t, frame.clock)
            if df in (17, 18) and len(hex_msg) == 28:
                # Same rule as the tracker, so evidence never describes a
                # ghost address from a corrupted frame.
                if hex_msg in addresses:
                    icao = addresses[hex_msg]
                else:
                    icao = addresses[hex_msg] = es_icao(hex_msg)
                if icao is None:
                    continue
                ev = self._ev(icao, t)
                # es_icao admits DF18 CF 2 / 6 only with IMF clear: TIS-B /
                # ADS-R relaying a real aircraft's ICAO address.
                if df == 18 and int(hex_msg[:2], 16) & 0x07 != 0:
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
                        ev.pos_clock[receiver].append((t, clock, _cpr_odd(hex_msg)))
                    key = (receiver, hex_msg)
                    seen = ev.recent_pos.get(key)
                    if seen is not None and 0 < t - seen <= _REPLAY_WINDOW:
                        ev.replays.append((t,))
                    ev.recent_pos[key] = t
                if clock is not None and receiver:
                    copies.setdefault(hex_msg, []).append((t, receiver, clock))
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
        for hex_msg, rows in copies.items():
            if len(rows) >= 2:
                # Only frames with an address are collected in copies.
                address = cast(str, addresses[hex_msg])
                self._pair_copies(self._evidence[address], rows)

    @staticmethod
    def _pair_copies(ev: AircraftEvidence, rows: list[tuple[float, str, int]]) -> None:
        """Group copies of one hex into transmissions by receive time.

        Velocity, identification and status squitters repeat bit-for-bit, so
        one receiver's copy of a transmission and another's copy of the next
        must not be paired (0.4 s apart: a ~120,000 km "path difference").
        Repeats are at least 0.4 s apart; copies of one transmission arrive
        within delivery jitter of each other.
        """
        rows.sort()
        start = rows[0][0]
        clocks: dict[str, int] = {}
        for t, receiver, clock in rows:
            if t - start > _COPY_WINDOW:
                if len(clocks) >= 2:
                    ev.add_tdoa(TdoaObservation(start, clocks))
                start, clocks = t, {}
            clocks.setdefault(receiver, clock)
        if len(clocks) >= 2:
            ev.add_tdoa(TdoaObservation(start, clocks))

    def _watch_clock(self, receiver: str, t: float, clock: int) -> None:
        """Resynchronise a receiver whose clock jumped (restart, replay loop)."""
        last = self._rx_clock.get(receiver)
        self._rx_clock[receiver] = (t, clock)
        if last is None:
            return
        # Fast path for every frame: clock_jump_seconds' limit is at least
        # CLOCK_JUMP_SECONDS, so anything inside it is never a jump.
        step = (clock - last[1] + _HALF_MOD) % BEAST_CLOCK_MOD - _HALF_MOD
        if abs(step / BEAST_CLOCK_HZ - (t - last[0])) <= CLOCK_JUMP_SECONDS:
            return
        jump = clock_jump_seconds(t - last[0], wrap_ticks(clock - last[1]))
        if jump is None:
            return
        # A feed mixing two clock domains would jump constantly: say so once.
        previous = self._clock_jumps.get(receiver)
        log.log(
            logging.INFO if previous is None or t - previous > 60.0 else logging.DEBUG,
            "Receiver %s clock jumped %+.1f s (restart or replay loop); resynchronising",
            receiver,
            jump,
        )
        self._clock_jumps[receiver] = t
        self._sync.reset_receiver(receiver, t)

    # --- evaluation ---------------------------------------------------------

    def evaluate(
        self, planes: dict[str, Plane], now: float | None = None, *, force: bool = False
    ) -> None:
        """Score the share of *planes* that is due; call once per tick.

        Each aircraft is processed once per ``_EVAL_INTERVAL`` at a phase set
        by its address, so the per-tick cost stays flat (at hz=2 half the
        aircraft come up each tick). A new aircraft is processed the first
        time it is seen, and one currently suspect or spoofed is re-checked
        at each of its slots rather than every ``_CHECK_INTERVAL``.
        Station-wide work (pruning, clock refit, calibration, clustering) runs
        once per interval. *force* processes and re-checks every aircraft now.
        """
        if not self.cfg.enabled:
            return
        now = time.time() if now is None else now
        station = force or now - self._last_eval >= _EVAL_INTERVAL
        if station:
            self._last_eval = now
        self._known = set(planes)
        cutoff = now - self.cfg.window_seconds
        antennas = self.positions.all()
        ecefs = {
            name: geodesy.ecef(p.latitude, p.longitude, p.altitude)
            for name, p in antennas.items()
        }

        if station:
            for icao, held in list(self._evidence.items()):
                if icao in planes:
                    continue  # trimmed in its own slot
                if held.last_seen < cutoff:
                    del self._evidence[icao]
                else:
                    self._trim(held, now, cutoff)
            for icao in [icao for icao in self._cached if icao not in planes]:
                self.forget(icao)

        batch = {icao: planes[icao] for icao in self._due(planes, now, force)}
        for icao, plane in batch.items():
            ev = self._evidence.get(icao)
            if ev is not None:
                self._trim(ev, now, cutoff)
                self._resolve(ev, plane, antennas)
            self._note_first_fix(icao, plane, ev)

        self._sync_clocks(batch, ecefs)
        if station:
            self._sync.refit(now)
        for icao, plane in batch.items():
            ev = self._evidence.get(icao)
            if ev is not None:
                self._tdoa_residuals(ev, plane, ecefs)
        checked = [icao for icao in batch if self._check_due(icao, force)]
        for icao in checked:
            self._stats[icao] = self._plane_stats(icao, now)
        if station:
            stats = {icao: self._stats[icao] for icao in planes if icao in self._stats}
            self._calibrate(planes, stats, now)
        for icao in checked:
            self._cached[icao] = self._check_plane(
                icao,
                batch[icao],
                self._evidence.get(icao),
                self._stats[icao],
                antennas,
                ecefs,
                now,
            )
        if station:
            results = {
                icao: dict(self._cached[icao][0])
                for icao in planes
                if icao in self._cached
            }
            self._clusters(results, {icao: self._stats[icao] for icao in results}, now)
            self._cluster_flags = {
                icao: found["emitter_cluster"]
                for icao, found in results.items()
                if "emitter_cluster" in found
            }

        for icao, plane in batch.items():
            found, emitter = self._cached[icao]
            flag = self._cluster_flags.get(icao)
            if flag is not None:
                found = {**found, "emitter_cluster": flag}
            self._publish(icao, plane, found, emitter, now)

    def _due(self, planes: dict[str, Plane], now: float, force: bool) -> list[str]:
        """Aircraft whose evaluation slot has come round since their last run."""
        due = []
        for icao in planes:
            phase = self._phase.get(icao)
            if phase is None:
                phase = zlib.crc32(icao.encode()) % 1000 / 1000 * _EVAL_INTERVAL
                self._phase[icao] = phase
            slot = math.floor((now - phase) / _EVAL_INTERVAL)
            if force or self._eval_slot.get(icao) != slot:
                self._eval_slot[icao] = slot
                due.append(icao)
        return due

    def _check_due(self, icao: str, force: bool) -> bool:
        """Full checks run in one of every _CHECK_SLOTS evaluation slots.

        Which one comes from address bits independent of the slot phase, so
        aircraft that appeared together still spread over the ticks.
        """
        if force or icao not in self._cached or self._statuses.get(icao) in _FAILING:
            return True
        offset = zlib.crc32(icao.encode()) // 1000 % _CHECK_SLOTS
        return (self._eval_slot[icao] + offset) % _CHECK_SLOTS == 0

    def _trim(self, ev: AircraftEvidence, now: float, cutoff: float) -> None:
        for group in (ev.rssi, ev.rssi_geo, ev.aoa, ev.aoa_geo, ev.pos_clock):
            for series in group.values():
                trim(series, cutoff)
        for series in (ev.cfo, ev.replays, ev.mode_s, ev.mode_s_alt):
            trim(series, cutoff)
        trim(ev.tdoa_resid, max(cutoff, now - _RECENT))
        while ev.tdoa and ev.tdoa[0].time < cutoff:
            ev.tdoa.popleft()
        stale = now - _REPLAY_WINDOW
        if ev.recent_pos:
            ev.recent_pos = {k: t for k, t in ev.recent_pos.items() if t >= stale}

    def _antenna(
        self, antennas: dict[str, ReceiverPosition], name: str
    ) -> ReceiverPosition:
        return antennas.get(name) or self.positions.get(name)

    def _resolve(
        self,
        ev: AircraftEvidence,
        plane: Plane,
        antennas: dict[str, ReceiverPosition],
    ) -> None:
        """Turn new raw RSSI / AoA samples into range and angle samples."""
        claimed = PositionLookup(plane)
        for receiver, raw in ev.rssi.items():
            if not raw:
                continue
            ant = self._antenna(antennas, receiver)
            origin = geodesy.ecef(ant.latitude, ant.longitude, ant.altitude)
            up = geodesy.up_vector(ant.latitude, ant.longitude)
            geo = ev.rssi_geo[receiver]
            last = geo[-1][0] if geo else -math.inf
            for t, rssi in raw:
                if t - last < GEO_SPACING:
                    continue
                pos = claimed.at(t)
                if pos is None:
                    continue
                alt = pos[2] if pos[2] is not None else ant.altitude
                target = geodesy.ecef(pos[0], pos[1], alt)
                slant_km = max(geodesy.slant_range_m(origin, target) / 1000.0, 0.01)
                elev = geodesy.elevation_between(origin, target, up)
                geo.append((t, rssi, slant_km, elev, 20 * math.log10(slant_km)))
                last = t
            raw.clear()
        for receiver, raw in ev.aoa.items():
            if not raw:
                continue
            ant = self._antenna(antennas, receiver)
            origin = geodesy.ecef(ant.latitude, ant.longitude, ant.altitude)
            up = geodesy.up_vector(ant.latitude, ant.longitude)
            geo = ev.aoa_geo[receiver]
            last = geo[-1][0] if geo else -math.inf
            for t, bearing, elevation in raw:
                if t - last < GEO_SPACING:
                    continue
                pos = claimed.at(t)
                if pos is None:
                    continue
                alt = pos[2] if pos[2] is not None else ant.altitude
                expected = geodesy.initial_bearing_deg(
                    ant.latitude, ant.longitude, pos[0], pos[1]
                )
                expected_elev = geodesy.elevation_between(
                    origin, geodesy.ecef(pos[0], pos[1], alt), up
                )
                geo.append((t, bearing, expected, elevation, expected_elev))
                last = t
            raw.clear()

    def _note_first_fix(
        self, icao: str, plane: Plane, ev: AircraftEvidence | None
    ) -> None:
        if icao in self._first_fix or ev is None:
            return
        recv = plane.received_data
        lats = recv.get(STORE_LAT) or []
        lons = recv.get(STORE_LONG) or []
        if not lats or not lons:
            return
        t0 = lats[0].time
        alt = _series_value(recv.get(STORE_ALT) or [], t0, 5.0)
        best: tuple[float, str] | None = None
        for receiver in ev.rx_last:
            ant = self.positions.get(receiver)
            km = geodesy.ground_distance_km(
                ant.latitude, ant.longitude, lats[0].value, lons[0].value
            )
            if best is None or km < best[0]:
                best = (km, receiver)
        if best is None:
            return
        km, receiver = best
        self._first_fix[icao] = (km, alt if alt is not None else math.nan, receiver, t0)
        if t0 - self._started >= _STARTUP_GRACE:
            self._first_fix_km[receiver].append(km)

    def _sync_clocks(
        self, planes: dict[str, Plane], ecefs: dict[str, geodesy.Vec3]
    ) -> None:
        """Feed frames from aircraft that currently look genuine into clock sync."""
        if len(ecefs) < 2:
            return
        for icao, plane in planes.items():
            ev = self._evidence.get(icao)
            if ev is None or not ev.tdoa:
                continue
            trusted = self._trusted(icao)
            newest = ev.tdoa_synced_until
            claimed = PositionLookup(plane) if trusted else None
            for obs in _newer_than(ev.tdoa, ev.tdoa_synced_until, key=_obs_time):
                newest = max(newest, obs.time)
                if claimed is None:
                    continue
                pos = claimed.at(obs.time, max_gap=1.0)
                if pos is None or pos[2] is None:
                    continue
                self._sync.add_sync(obs, cast(_Fix3D, pos), ecefs, icao)
            ev.tdoa_synced_until = newest

    def _tdoa_residuals(
        self, ev: AircraftEvidence, plane: Plane, ecefs: dict[str, geodesy.Vec3]
    ) -> None:
        """Test each new multi-receiver observation once against the claim."""
        if not ev.tdoa or len(ecefs) < 2:
            return
        max_spread = self.cfg.tdoa_tolerance_m / 2
        fresh = _newer_than(ev.tdoa, ev.tdoa_cursor, key=_obs_time)
        if fresh:
            ev.tdoa_cursor = max(obs.time for obs in fresh)
        last = ev.tdoa_resid[-1][0] if ev.tdoa_resid else -math.inf
        claimed = PositionLookup(plane)
        for obs in fresh:
            if obs.time - last < GEO_SPACING:
                continue
            pos = claimed.at(obs.time, max_gap=1.0)
            if pos is None or pos[2] is None:
                continue
            residuals = self._sync.residuals_m(
                obs, cast(_Fix3D, pos), ecefs, max_spread
            )
            for residual in residuals:
                ev.tdoa_resid.append((obs.time, residual))
            if residuals:
                last = obs.time
        # Clock sync ran first this tick, so older frames are spent.
        while len(ev.tdoa) > _TDOA_KEEP:
            ev.tdoa.popleft()

    def _plane_stats(self, icao: str, now: float) -> _PlaneStats:
        ev = self._evidence.get(icao)
        stats = _PlaneStats({}, {}, {}, {})
        if ev is None:
            return stats
        recent = now - _RECENT
        for receiver, rows in ev.rssi_geo.items():
            current = rows_since(rows, recent)
            usable = [r for r in current if r[1] < _SATURATED_DBFS]
            if len(usable) < 3:
                if len(current) >= 3:
                    # Clipped samples understate the signal, so they can
                    # still show it is too strong (a nearby transmitter).
                    stats.rssi_floor[receiver] = fast_median(
                        r[1] + r[4] for r in current
                    )
                continue
            stats.rssi_index[receiver] = fast_median(r[1] + r[4] for r in usable)
            stats.rssi_median[receiver] = fast_median(r[1] for r in usable)
        for receiver, rows in ev.aoa_geo.items():
            corrected = [
                self._corrected_bearing(receiver, row[1])
                for row in rows_since(rows, recent)
            ]
            if len(corrected) >= 5:
                median = circular_median_deg(corrected)
                if median is not None:
                    stats.bearing_median[receiver] = median
        return stats

    def _calibrate(
        self, planes: dict[str, Plane], stats: dict[str, _PlaneStats], now: float
    ) -> None:
        trusted = [icao for icao in planes if icao in stats and self._trusted(icao)]
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

        # AoA bias (array orientation) for receivers without a configured
        # offset. A linear array's axis only resolves the mirror ambiguity;
        # its mounting error still has to be learned.
        recent = now - _RECENT
        aoa_trusted = [
            icao
            for icao in planes
            if icao in self._established
            and self._aoa_trust.get(icao, 0.0) >= self.cfg.ok_score
        ]
        for receiver, (offset, axis) in self._aoa_options.items():
            if offset is not None:
                continue
            per_plane: list[tuple[float, ...]] = []
            for icao in aoa_trusted:
                ev = self._evidence.get(icao)
                rows = ev.aoa_geo.get(receiver) if ev is not None else None
                if not rows:
                    continue
                recent_rows = [(m, e) for t, m, e, _el, _eel in rows if t >= recent]
                if len(recent_rows) < 5:
                    continue
                # Circular: an array mounted backwards has diffs near ±180.
                direct = circular_median_deg(
                    [geodesy.angle_diff_deg(m, e) for m, e in recent_rows]
                )
                assert direct is not None  # recent_rows is not empty
                if axis is None:
                    per_plane.append((direct,))
                    continue
                # A linear array may report the reflection of the bearing
                # about its axis: then the bias is measured - mirror(expected).
                mirrored = circular_median_deg(
                    [
                        geodesy.angle_diff_deg(m, (2 * axis - e) % 360.0)
                        for m, e in recent_rows
                    ]
                )
                assert mirrored is not None
                per_plane.append((direct, mirrored))
            if len(per_plane) >= _MIN_BIAS_AIRCRAFT:
                if axis is None:
                    bias = circular_median_deg([p[0] for p in per_plane])
                else:
                    bias = _consensus_angle(
                        per_plane, _BIAS_AGREE_DEG, _MIN_BIAS_AIRCRAFT
                    )
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

    def _trusted(self, icao: str) -> bool:
        # ok needs enough evidence, and worsening is immediate, so a new or
        # currently failing aircraft never calibrates the station models.
        return self._statuses.get(icao) == STATUS_OK

    def _corroboration_eligible(self, plane: Plane, now: float) -> bool:
        if now - _track_started(plane.track) < _ELIGIBLE_SECONDS:
            return False
        pkt = plane.track.packet_types
        return pkt.get(CAT_AIRBORNE_BARO, 0) + pkt.get(CAT_AIRBORNE_GNSS, 0) >= 20

    def _corrected_bearing(self, receiver: str, measured: float) -> float:
        offset, _axis = self._aoa_options.get(receiver, (None, None))
        if offset is not None:
            return (measured + offset) % 360.0
        return (measured - self._bearing_bias.get(receiver, 0.0)) % 360.0

    def _orientation_known(self, receiver: str) -> bool:
        """Configured, or already learned, so bearings can be judged."""
        offset, _axis = self._aoa_options.get(receiver, (None, None))
        return offset is not None or receiver in self._bearing_bias

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
        plane: Plane,
        ev: AircraftEvidence | None,
        stats: _PlaneStats,
        antennas: dict[str, ReceiverPosition],
        ecefs: dict[str, geodesy.Vec3],
        now: float,
    ) -> tuple[dict[str, CheckResult], dict[str, float] | None]:
        cfg = self.cfg
        out: dict[str, CheckResult | None] = {}
        emitter: dict[str, float] | None = None
        info = plane.info
        track = plane.track
        recv = plane.received_data
        pkt = track.packet_types
        first_packet = _track_started(track)
        fix = latest_fix(plane)
        recent = now - _RECENT

        out["identity"] = checks.identity(
            icao, info.callsign, info.registration
        )
        positions = pkt.get(CAT_AIRBORNE_BARO, 0) + pkt.get(CAT_AIRBORNE_GNSS, 0)
        out["duplicate_icao"] = checks.duplicate_icao(
            track.gate_rejects,
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
                    km = geodesy.ground_distance_km(
                        ant.latitude, ant.longitude, fix[0], fix[1]
                    )
                    limit = (
                        geodesy.radio_horizon_km(
                            max(ant.antenna_height, ant.altitude), fix[2]
                        )
                        * cfg.horizon_margin
                    )
                    distances.append((receiver, km, limit))
                out["radio_horizon"] = checks.radio_horizon(distances, cfg.max_range_km)

            excess = [
                (receiver, index - self._rssi_model[receiver])
                for receiver, index in stats.rssi_index.items()
                if receiver in self._rssi_model
            ]
            excess += [
                (receiver, floor - self._rssi_model[receiver])
                for receiver, floor in stats.rssi_floor.items()
                if receiver in self._rssi_model
                and floor - self._rssi_model[receiver] > cfg.rssi_excess_db
            ]
            out["rssi_range"] = checks.rssi_range(excess, cfg.rssi_excess_db)
            out["rssi_trend"] = self._rssi_trend(ev)
            out["aoa"] = self._aoa(ev, recent)
            tdoa_result, emitter = self._tdoa(icao, plane, ev, fix, ecefs, now)
            out["tdoa"] = tdoa_result
            out["mode_s_altitude"] = self._mode_s_altitude(ev, recv)
            if self._corroboration_rate is not None and self._corroboration_eligible(
                plane, now
            ):
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
                out["appearance"] = checks.appearance(
                    km, alt, statistics.median(typical)
                )

        if (
            self._network is not None
            and fix is not None
            and now - first_packet >= _RECENT
        ):
            self._network.request(icao, now)
            sighting = self._network.result(icao)
            if sighting is not None:
                distance = None
                if (
                    sighting.found
                    and sighting.latitude is not None
                    and sighting.longitude is not None
                ):
                    here = position_at(
                        plane, sighting.position_time or now, max_gap=10.0
                    )
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

        if self._rebroadcast_only(ev, now):
            out = {name: r for name, r in out.items() if name not in _RF_CHECKS}
            emitter = None
        return {
            name: result for name, result in out.items() if result is not None
        }, emitter

    @staticmethod
    def _rebroadcast_only(ev: AircraftEvidence | None, now: float) -> bool:
        # Direct DF17 frames still describe the aircraft's own transmitter;
        # otherwise one TIS-B frame would switch off the RF checks.
        if ev is None or not ev.rebroadcast:
            return False
        return all(t < now - _RECENT for t in ev.rx_last.values())

    def _kinematics(
        self, plane: Plane, fix: tuple[float, float, float | None, float] | None
    ) -> CheckResult | None:
        if fix is None:
            return None
        recv = plane.received_data
        lat_series = recv.get(STORE_LAT) or []
        lon_series = recv.get(STORE_LONG) or []
        t1 = fix[3]
        target = t1 - 15.0
        lat0 = nearest_datum(lat_series, target)
        lon0 = nearest_datum(lon_series, target)
        if lat0 is None or lon0 is None:
            return None
        t0 = (
            lat0.time
            if abs(lat0.time - target) <= abs(lon0.time - target)
            else lon0.time
        )
        elapsed = t1 - t0
        if not 8.0 <= elapsed <= 30.0:
            return None
        distance_m = (
            geodesy.ground_distance_km(lat0.value, lon0.value, fix[0], fix[1]) * 1000
        )
        bearing = (
            geodesy.initial_bearing_deg(lat0.value, lon0.value, fix[0], fix[1])
            if distance_m > 0
            else None
        )
        speeds = [
            d.value for d in series_window(recv.get(STORE_HORIZ_SPEED) or [], t0, t1)
        ]
        tracks = [d.value for d in series_window(recv.get(STORE_HEADING) or [], t0, t1)]
        vrates = [
            d.value for d in series_window(recv.get(STORE_VERT_SPEED) or [], t0, t1)
        ]
        alt_series = recv.get(STORE_ALT) or []
        alt0 = _series_value(alt_series, t0, 3.0)
        alt1 = _series_value(alt_series, t1, 3.0)
        climb = (
            (alt1 - alt0) / elapsed if alt0 is not None and alt1 is not None else None
        )
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
                (path_db, rssi)
                for _t, rssi, _slant, elev, path_db in rows
                if rssi < _SATURATED_DBFS and elev < _TREND_MAX_ELEVATION
            ]
            best = checks.worst([best, checks.rssi_trend(samples)])
        return best

    def _aoa(self, ev: AircraftEvidence, recent: float) -> CheckResult | None:
        found: list[CheckResult | None] = []
        for receiver, rows in ev.aoa_geo.items():
            if not self._orientation_known(receiver):
                continue
            bearing_errors: list[float] = []
            elevation_errors: list[float] = []
            for t, measured, expected, elev, expected_elev in rows:
                if t < recent:
                    continue
                bearing_errors.append(self._bearing_error(receiver, measured, expected))
                if elev is not None:
                    elevation_errors.append(elev - expected_elev)
            found.append(
                checks.aoa(
                    bearing_errors,
                    elevation_errors,
                    self.cfg.aoa_tolerance_deg,
                    receiver,
                )
            )
        return checks.worst(found)

    def _tdoa(
        self,
        icao: str,
        plane: Plane,
        ev: AircraftEvidence,
        fix: tuple[float, float, float | None, float] | None,
        ecefs: dict[str, geodesy.Vec3],
        now: float,
    ) -> tuple[CheckResult | None, dict[str, float] | None]:
        if not ev.tdoa or len(ecefs) < 2:
            return None, None
        recent = now - _RECENT
        tolerance = self.cfg.tdoa_tolerance_m
        max_spread = tolerance / 2
        residuals = [r for _t, r in rows_since(ev.tdoa_resid, recent)]
        result = checks.tdoa(residuals, tolerance)
        if result is None or result.status != FAIL:
            return result, None
        self._sync.drop_aircraft(icao, now)
        # Locate the real transmitter only for a target that already fails:
        # far outside the receiver baseline the geometry is too weak for the
        # solution itself to be evidence.
        if fix is None:
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

    def _mode_s_altitude(self, ev: AircraftEvidence, recv: dict) -> CheckResult | None:
        # Mode S replies carry barometric altitude, and so does ``altitude``
        # (GNSS height from TC 20-22 positions is kept apart, in gnss_height).
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
            for group in _groups(rows, _BEARING_CLUSTER_DEG, min_size=2, period=360.0):
                centre = circular_median_deg([v for v, _ in group]) or 0.0
                for _v, icao in group:
                    results[icao].setdefault(
                        "emitter_cluster",
                        checks.emitter_cluster(
                            f"{receiver} bearing {centre:.0f}°", len(group)
                        ),
                    )

    # --- output -------------------------------------------------------------

    def _publish(
        self,
        icao: str,
        plane: Plane,
        found: dict[str, CheckResult],
        emitter: dict[str, float] | None,
        now: float,
    ) -> None:
        score = 1.0
        aoa_trust = 1.0
        weighted: dict[str, CheckResult] = {}
        for name, result in found.items():
            weight = self.cfg.weight(name)
            if weight <= 0:
                continue
            weighted[name] = result
            factor = 1.0 - weight * _LEVEL.get(result.status, 0.0)
            score *= factor
            if name != "aoa":
                aoa_trust *= factor
        self._aoa_trust[icao] = aoa_trust
        # Status follows the published score, so the two never disagree.
        score = round(score, 2)
        rebroadcast = self._rebroadcast_only(self._evidence.get(icao), now)
        first_packet = _track_started(plane.track)
        min_checks = _TRUST_MIN_CHECKS_REBROADCAST if rebroadcast else _TRUST_MIN_CHECKS
        if now - first_packet >= _TRUST_MIN_SECONDS and len(weighted) >= min_checks:
            self._established.add(icao)
        if not weighted:
            raw = STATUS_UNKNOWN
        elif score >= self.cfg.ok_score:
            # A few early passes are not verification; a failure is evidence.
            raw = STATUS_OK if icao in self._established else STATUS_UNKNOWN
        elif score < self.cfg.spoofed_score:
            raw = STATUS_SPOOFED
        else:
            raw = STATUS_SUSPECT
        self._scores[icao] = score

        previous = self._statuses.get(icao)
        status = raw
        if (
            previous is not None
            and _SEVERITY[raw] < _SEVERITY[previous]
            and now - self._held_at.get(icao, now) < _RECOVER_SECONDS
        ):
            status = previous
        if _SEVERITY[raw] >= _SEVERITY[status]:
            self._held_at[icao] = now
        self._statuses[icao] = status
        if status == STATUS_SPOOFED and previous != STATUS_SPOOFED:
            reasons = "; ".join(
                f"{name}: {r.detail}"
                for name, r in weighted.items()
                if r.status == FAIL
            )
            log.warning(
                "Integrity: %s %s looks spoofed (score %.2f): %s",
                icao,
                plane.info.callsign or "",
                score,
                reasons,
            )

        signature = (
            status,
            score,
            tuple(sorted((name, r.status) for name, r in weighted.items())),
            emitter is not None,
            rebroadcast,
        )
        changed = signature != self._signatures.get(icao)
        # Status changes are logged at once (hysteresis bounds them); score or
        # check-only changes at most every _REWRITE_SECONDS, so one check
        # flapping under a held status cannot flood the integrity table.
        logged = self._logged.get(icao)
        log_it = (
            logged is None
            or status != logged[0]
            or (signature != logged[1] and now - logged[2] >= _REWRITE_SECONDS)
        )
        if (
            not changed
            and not log_it
            and now - self._written_at.get(icao, 0.0) < _REWRITE_SECONDS
        ):
            return
        self._signatures[icao] = signature
        self._written_at[icao] = now
        checks_doc = {name: r.as_dict() for name, r in sorted(weighted.items())}
        # An unknown verdict has no meaningful score; publishing the 1.0
        # placeholder would archive unverified samples as fully consistent.
        published = None if status == STATUS_UNKNOWN else score
        doc: IntegrityDoc = {
            "score": published,
            "status": status,
            "checks": checks_doc,
        }
        if emitter is not None:
            doc["emitter"] = emitter
        if signature[-1]:
            doc["rebroadcast"] = True
        plane.info.integrity = doc
        if not log_it:
            return
        self._logged[icao] = (status, signature, now)
        # The archive labels each telemetry sample with the verdict in effect
        # when it arrived, and keeps every verdict in the integrity table.
        flagged = tuple(name for name, r in checks_doc.items() if r["status"] != PASS)
        plane.track.integrity_log.append(
            Verdict(now, published, status, flagged, checks_doc)
        )

    def score(self, icao: str) -> float | None:
        return self._scores.get(icao.upper())


def _consensus_angle(
    candidates: list[tuple[float, ...]], tolerance: float, quorum: int
) -> float | None:
    """The angle most items agree on, each offering a few candidates.

    For a linear array each aircraft offers the bias read directly and read
    as a reflection. Only the true bias is common to aircraft at different
    bearings; the other candidate moves with each aircraft's bearing.
    """
    best: list[float] = []
    for anchor in (c for item in candidates for c in item):
        agreeing = []
        for item in candidates:
            nearest = min(item, key=lambda c: abs(geodesy.angle_diff_deg(c, anchor)))
            if abs(geodesy.angle_diff_deg(nearest, anchor)) <= tolerance:
                agreeing.append(nearest)
        if len(agreeing) > len(best):
            best = agreeing
    if len(best) < quorum:
        return None
    return circular_median_deg(best)


def _newer_than(series: deque, cursor: float, *, key=None) -> list:
    """Samples with time > *cursor*, oldest first (walks back from the end)."""
    fresh = []
    for row in reversed(series):
        if (key(row) if key is not None else row[0]) <= cursor:
            break
        fresh.append(row)
    fresh.reverse()
    return fresh


def _obs_time(obs: TdoaObservation) -> float:
    return obs.time


def _groups(
    rows: list[tuple[float, str]],
    tolerance: float,
    *,
    min_size: int = 3,
    period: float | None = None,
) -> list[list[tuple[float, str]]]:
    """Chains of sorted values whose neighbours are within *tolerance*.

    With *period* (360 for bearings) values are circular, so a chain may
    continue across the wrap (359° next to 1°).
    """
    if len(rows) < min_size:
        return []
    ordered = sorted(rows)
    chains: list[list[tuple[float, str]]] = []
    current = [ordered[0]]
    for row in ordered[1:]:
        if row[0] - current[-1][0] <= tolerance:
            current.append(row)
        else:
            chains.append(current)
            current = [row]
    chains.append(current)
    if (
        period is not None
        and len(chains) > 1
        and ordered[0][0] + period - ordered[-1][0] <= tolerance
    ):
        chains[0] = chains.pop() + chains[0]
    return [chain for chain in chains if len(chain) >= min_size]
