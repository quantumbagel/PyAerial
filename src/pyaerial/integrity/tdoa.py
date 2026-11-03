"""Time-difference-of-arrival between receivers with free-running 12 MHz clocks.

Each receiver stamps frames with its own sample clock, so the clocks first have
to be related. For every receiver pair the offset between the clocks is
learned from aircraft we already trust: their position predicts how much
earlier one antenna should hear a frame, and what is left over is the clock
offset (plus drift). Once a pair is synchronised, any aircraft's frames can be
tested the other way round: the measured time difference must match the
position the aircraft claims. With three or more receivers the frame times
also locate the transmitter (multilateration), which for a spoofed target is
where the spoofer actually is.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from pyaerial.integrity import geodesy
from pyaerial.integrity.stats import fast_median, mad, robust_line
from pyaerial.receivers.frames import BEAST_CLOCK_HZ, BEAST_CLOCK_MOD

_HALF_MOD = BEAST_CLOCK_MOD // 2
METRES_PER_TICK = geodesy.SPEED_OF_LIGHT / BEAST_CLOCK_HZ
_SYNC_WINDOW_SECONDS = 30.0
_SYNC_MAX_POINTS = 400
# One point per pair per slot, so the points span the whole window however
# busy the sky is (a 400-point FIFO covered ~0.2 s with 300 aircraft).
_SYNC_SLOT_SECONDS = _SYNC_WINDOW_SECONDS / _SYNC_MAX_POINTS
_SYNC_MIN_POINTS = 10
_SYNC_MIN_AIRCRAFT = 2
# Recent sync points this far off the model mean a receiver restarted.
_RESET_RESIDUAL_M = 50_000.0
_RESET_STREAK = 5
# An aircraft whose sync points sit this far from the other aircraft's is
# lying about its position (or mis-decoded): genuine ones agree to metres.
_AIRCRAFT_OUTLIER_M = 2_000.0
# A receiver's clock stepping this much more (or less) than wall time is a
# discontinuity: a restart or a replay loop, not delivery jitter. Long gaps
# also allow for crystal drift.
CLOCK_JUMP_SECONDS = 2.0
CLOCK_JUMP_DRIFT = 200e-6
# A pair's implied path difference can never exceed the antenna baseline;
# beyond it (plus model error) the copies were not one transmission.
_PAIR_SLACK_M = 5_000.0
_HEIGHT_WEIGHT = 10.0
_MAX_STEP_M = 20_000.0


def wrap_ticks(value: float) -> float:
    """Map a 48-bit clock difference into the signed range."""
    return ((value + _HALF_MOD) % BEAST_CLOCK_MOD) - _HALF_MOD


def clock_jump_seconds(wall_step: float, clock_step_ticks: float) -> float | None:
    """How far a clock step disagrees with the wall step, if it is a jump."""
    error = clock_step_ticks / BEAST_CLOCK_HZ - wall_step
    limit = max(CLOCK_JUMP_SECONDS, CLOCK_JUMP_DRIFT * abs(wall_step))
    return error if abs(error) > limit else None


@dataclass(slots=True)
class PairModel:
    """Clock offset ``clock_a - clock_b`` as a line in wall time."""

    a: str
    b: str
    points: deque = field(default_factory=lambda: deque(maxlen=_SYNC_MAX_POINTS))
    intercept: float = 0.0
    slope: float = 0.0
    t_ref: float = 0.0
    spread_m: float = float("inf")
    fitted: bool = False
    _bad_streak: int = 0
    # Aircraft behind the current streak of far-off points
    _bad_aircraft: set[str] = field(default_factory=set)
    # Candidates since the last refit: slot -> [(t, offset_ticks, icao)]
    _pending: dict[int, list[tuple[float, float, str]]] = field(default_factory=dict)
    # Slots that already hold a point. Not a high-water mark: aircraft are
    # scored on different ticks, so one refit may receive points older than
    # those admitted at the previous one.
    _admitted: set[int] = field(default_factory=set)

    def predict(self, t: float) -> float:
        return self.intercept + self.slope * (t - self.t_ref)

    def add(self, t: float, offset_ticks: float, icao: str) -> None:
        if self.fitted:
            # Unwrap against the model so the 48-bit wrap never splits the fit.
            pred = self.predict(t)
            residual = wrap_ticks(offset_ticks - pred)
            offset_ticks = pred + residual
            if abs(residual) * METRES_PER_TICK > _RESET_RESIDUAL_M:
                # A restart moves every aircraft's points; one aircraft's
                # (whose frames arrive back to back) is that aircraft lying.
                self._bad_streak += 1
                self._bad_aircraft.add(icao)
                if self._bad_streak >= _RESET_STREAK and len(self._bad_aircraft) >= 2:
                    self.reset()
                return
            self._bad_streak = 0
            self._bad_aircraft.clear()
        else:
            ref = self._last_offset()
            if ref is not None:
                offset_ticks = ref + wrap_ticks(offset_ticks - ref)
        slot = int(t // _SYNC_SLOT_SECONDS)
        if slot in self._admitted:
            return
        self._pending.setdefault(slot, []).append((t, offset_ticks, icao))

    def reset(self) -> None:
        self.points.clear()
        self._pending.clear()
        self.fitted = False
        self.spread_m = float("inf")
        self._bad_streak = 0
        self._bad_aircraft.clear()
        self._admitted.clear()

    def _last_offset(self) -> float | None:
        if self._pending:
            return self._pending[max(self._pending)][-1][1]
        return self.points[-1][1] if self.points else None

    def _admit(self) -> None:
        """Keep one candidate per slot, from the aircraft with fewest points,
        so no single aircraft's geometry (or position error) dominates."""
        counts: dict[str, int] = {}
        for _t, _y, icao in self.points:
            counts[icao] = counts.get(icao, 0) + 1
        if not self._pending:
            return
        late = bool(self.points) and min(self._pending) * _SYNC_SLOT_SECONDS < self.points[-1][0]
        for slot in sorted(self._pending):
            point = min(self._pending[slot], key=lambda p: (counts.get(p[2], 0), p[0]))
            counts[point[2]] = counts.get(point[2], 0) + 1
            self.points.append(point)
            self._admitted.add(slot)
        self._pending.clear()
        if late:
            # Keep time order: trimming pops the oldest from the left, and
            # the fit is referenced to the newest point.
            self.points = deque(sorted(self.points), maxlen=_SYNC_MAX_POINTS)

    def refit(self, now: float) -> None:
        self._admit()
        while self.points and self.points[0][0] < now - _SYNC_WINDOW_SECONDS:
            self.points.popleft()
        if len(self._admitted) > len(self.points):
            self._admitted = {int(p[0] // _SYNC_SLOT_SECONDS) for p in self.points}
        aircraft = {icao for _t, _y, icao in self.points}
        if len(self.points) < _SYNC_MIN_POINTS or len(aircraft) < _SYNC_MIN_AIRCRAFT:
            self.fitted = False
            self.spread_m = float("inf")
            return
        t_ref = self.points[-1][0]
        rows = [(t - t_ref, y, icao) for t, y, icao in self.points]
        # Point-wise outlier rejection cannot remove one aircraft's whole
        # cluster, and right after start-up (or a reset) a spoofer trusted
        # alongside a handful of genuine aircraft is a large share of the
        # points. Drop aircraft that disagree with the consensus, twice
        # (the first line may itself be pulled by them).
        for _ in range(2):
            fit, residuals = _fit(rows)
            liars = _outlier_aircraft(rows, residuals)
            if not liars:
                break
            rows = [row for row in rows if row[2] not in liars]
            if len(rows) < _SYNC_MIN_POINTS or len({r[2] for r in rows}) < _SYNC_MIN_AIRCRAFT:
                # e.g. two aircraft that disagree: no telling which is right
                self.fitted = False
                self.spread_m = float("inf")
                return
        else:
            fit, residuals = _fit(rows)
        self.intercept, self.slope = fit
        self.t_ref = t_ref
        self.spread_m = 1.4826 * mad(residuals) * METRES_PER_TICK
        self.fitted = True


def _fit(rows: list[tuple[float, float, str]]) -> tuple[tuple[float, float], list[float]]:
    xs = [x for x, _y, _icao in rows]
    ys = [y for _x, y, _icao in rows]
    fit = robust_line(xs, ys)
    if fit is None:
        # All points at one instant: offset only.
        fit = (sum(ys) / len(ys), 0.0)
    intercept, slope = fit
    return fit, [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]


def _outlier_aircraft(rows: list[tuple[float, float, str]], residuals: list[float]) -> set[str]:
    """Aircraft whose median residual is far from the median aircraft's."""
    by: dict[str, list[float]] = {}
    for (_x, _y, icao), residual in zip(rows, residuals, strict=True):
        by.setdefault(icao, []).append(residual)
    if len(by) < 2:
        return set()
    medians = {icao: fast_median(values) for icao, values in by.items()}
    consensus = fast_median(medians.values())
    limit = _AIRCRAFT_OUTLIER_M / METRES_PER_TICK
    return {icao for icao, m in medians.items() if abs(m - consensus) > limit}


@dataclass(frozen=True, slots=True)
class TdoaObservation:
    """One transmission heard by several receivers: wall time and raw clocks."""

    time: float
    clocks: dict[str, int]


class ClockSync:
    """All receiver-pair models."""

    def __init__(self) -> None:
        self.pairs: dict[tuple[str, str], PairModel] = {}
        # receiver -> wall time its clock last jumped; earlier clocks are void.
        self._epoch: dict[str, float] = {}

    def drop_aircraft(self, icao: str, now: float) -> None:
        """Remove *icao*'s sync points and refit: its TDOA contradicts its
        claim, so frames it fed while still trusted (a spoofer passing the
        bootstrap) would bias every clock model it touched."""
        for model in self.pairs.values():
            pending = {
                slot: kept
                for slot, rows in model._pending.items()
                if (kept := [p for p in rows if p[2] != icao])
            }
            if len(pending) == len(model._pending) and all(
                p[2] != icao for p in model.points
            ):
                continue
            model._pending = pending
            model.points = deque(
                (p for p in model.points if p[2] != icao), maxlen=_SYNC_MAX_POINTS
            )
            model.refit(now)

    def reset_receiver(self, name: str, at: float) -> None:
        """Forget every pair with *name*, whose clock just jumped at *at*."""
        self._epoch[name] = at
        for (a, b), model in self.pairs.items():
            if name in (a, b):
                model.reset()

    def _names(self, obs: TdoaObservation, antennas: dict[str, geodesy.Vec3]) -> list[str]:
        if not self._epoch:
            return sorted(name for name in obs.clocks if name in antennas)
        return sorted(
            name
            for name in obs.clocks
            if name in antennas and obs.time >= self._epoch.get(name, -math.inf)
        )

    def _pair(self, a: str, b: str) -> PairModel:
        key = (a, b)
        model = self.pairs.get(key)
        if model is None:
            model = PairModel(a, b)
            self.pairs[key] = model
        return model

    def add_sync(
        self,
        obs: TdoaObservation,
        position: tuple[float, float, float],
        antennas: dict[str, geodesy.Vec3],
        icao: str,
    ) -> None:
        """Feed a trusted aircraft's frame into every pair that heard it."""
        target = geodesy.ecef(*position)
        names = self._names(obs, antennas)
        ranges = {name: geodesy.slant_range_m(antennas[name], target) for name in names}
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                expected = (ranges[a] - ranges[b]) / METRES_PER_TICK
                raw = wrap_ticks(obs.clocks[a] - obs.clocks[b])
                self._pair(a, b).add(obs.time, raw - expected, icao)

    def refit(self, now: float) -> None:
        for model in self.pairs.values():
            model.refit(now)

    def synced(self, a: str, b: str, max_spread_m: float) -> PairModel | None:
        model = self.pairs.get((a, b) if a < b else (b, a))
        if model is None or not model.fitted or model.spread_m > max_spread_m:
            return None
        return model

    def residuals_m(
        self,
        obs: TdoaObservation,
        position: tuple[float, float, float],
        antennas: dict[str, geodesy.Vec3],
        max_spread_m: float,
    ) -> list[float]:
        """Path-difference error (m) of *position* for each synced pair."""
        target = geodesy.ecef(*position)
        names = self._names(obs, antennas)
        out: list[float] = []
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                model = self.synced(a, b, max_spread_m)
                if model is None:
                    continue
                expected = (
                    geodesy.slant_range_m(antennas[a], target)
                    - geodesy.slant_range_m(antennas[b], target)
                ) / METRES_PER_TICK
                raw = wrap_ticks(obs.clocks[a] - obs.clocks[b])
                implied = wrap_ticks(raw - model.predict(obs.time)) * METRES_PER_TICK
                if abs(implied) > math.dist(antennas[a], antennas[b]) + _PAIR_SLACK_M:
                    continue
                out.append(wrap_ticks(raw - expected - model.predict(obs.time)) * METRES_PER_TICK)
        return out

    def range_differences(
        self, obs: TdoaObservation, antennas: dict[str, geodesy.Vec3], max_spread_m: float
    ) -> tuple[str, list[tuple[str, float]]] | None:
        """``(reference, [(receiver, r_receiver - r_reference)])`` in metres."""
        names = self._names(obs, antennas)
        if len(names) < 3:
            return None
        ref = names[0]
        diffs: list[tuple[str, float]] = []
        for other in names[1:]:
            model = self.synced(ref, other, max_spread_m)
            if model is None:
                continue
            # model: clock_ref - clock_other - (tau_ref - tau_other) = offset
            raw = wrap_ticks(obs.clocks[ref] - obs.clocks[other])
            tau_ref_minus_other = wrap_ticks(raw - model.predict(obs.time))
            limit = math.dist(antennas[ref], antennas[other]) + _PAIR_SLACK_M
            if abs(tau_ref_minus_other) * METRES_PER_TICK > limit:
                continue
            diffs.append((other, -tau_ref_minus_other * METRES_PER_TICK))
        if len(diffs) < 2:
            return None
        return ref, diffs


def multilaterate(
    reference: geodesy.Vec3,
    others: list[tuple[geodesy.Vec3, float]],
    start: geodesy.Vec3,
    *,
    fixed_altitude: float,
    iterations: int = 30,
) -> tuple[geodesy.Vec3, float] | None:
    """Gauss-Newton fit of an emitter at *fixed_altitude* to range differences.

    *others* holds ``(antenna_ecef, r_antenna - r_reference)``. Ground
    antennas are nearly coplanar, so height is all but unobservable and a free
    3-D solve wanders off; the caller tries candidate heights (claimed
    altitude, ground level) and keeps the lowest residual. Returns
    ``(ecef, rms_m)`` where rms covers the range differences only.
    """
    ref = np.asarray(reference, dtype=float)
    points = np.asarray([o[0] for o in others], dtype=float)
    measured = np.asarray([o[1] for o in others], dtype=float)
    x = np.asarray(start, dtype=float)
    for _ in range(iterations):
        r_ref = np.linalg.norm(x - ref)
        r_pts = np.linalg.norm(points - x, axis=1)
        if r_ref == 0 or np.any(r_pts == 0):
            return None
        residual = (r_pts - r_ref) - measured
        jac = (x - points) / r_pts[:, None] - (x - ref) / r_ref
        lat, lon, alt = geodesy.geodetic(*x)
        up = np.asarray(geodesy.ecef(lat, lon, alt + 1.0)) - x
        jac = np.vstack([jac, _HEIGHT_WEIGHT * up / np.linalg.norm(up)])
        residual = np.append(residual, _HEIGHT_WEIGHT * (alt - fixed_altitude))
        step, *_ = np.linalg.lstsq(jac, -residual, rcond=None)
        norm = float(np.linalg.norm(step))
        if norm > _MAX_STEP_M:
            step *= _MAX_STEP_M / norm
        x = x + step
        if norm < 0.5:
            break
    if not np.all(np.isfinite(x)):
        return None
    r_ref = np.linalg.norm(x - ref)
    r_pts = np.linalg.norm(points - x, axis=1)
    rms = float(np.sqrt(np.mean(((r_pts - r_ref) - measured) ** 2)))
    return (float(x[0]), float(x[1]), float(x[2])), rms
