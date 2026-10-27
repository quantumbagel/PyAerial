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

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from pyaerial.integrity import geodesy
from pyaerial.integrity.stats import mad, robust_line
from pyaerial.receivers.frames import BEAST_CLOCK_HZ, BEAST_CLOCK_MOD

_HALF_MOD = BEAST_CLOCK_MOD // 2
METRES_PER_TICK = geodesy.SPEED_OF_LIGHT / BEAST_CLOCK_HZ
_SYNC_WINDOW_SECONDS = 30.0
_SYNC_MAX_POINTS = 400
_SYNC_MIN_POINTS = 10
_SYNC_MIN_AIRCRAFT = 2
# Recent sync points this far off the model mean a receiver restarted.
_RESET_RESIDUAL_M = 50_000.0
_RESET_STREAK = 5
_HEIGHT_WEIGHT = 10.0
_MAX_STEP_M = 20_000.0


def wrap_ticks(value: float) -> float:
    """Map a 48-bit clock difference into the signed range."""
    return ((value + _HALF_MOD) % BEAST_CLOCK_MOD) - _HALF_MOD


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

    def predict(self, t: float) -> float:
        return self.intercept + self.slope * (t - self.t_ref)

    def add(self, t: float, offset_ticks: float, icao: str) -> None:
        if self.fitted:
            # Unwrap against the model so the 48-bit wrap never splits the fit.
            pred = self.predict(t)
            residual = wrap_ticks(offset_ticks - pred)
            offset_ticks = pred + residual
            if abs(residual) * METRES_PER_TICK > _RESET_RESIDUAL_M:
                self._bad_streak += 1
                if self._bad_streak >= _RESET_STREAK:
                    self.points.clear()
                    self.fitted = False
                    self._bad_streak = 0
                return
            self._bad_streak = 0
        elif self.points:
            ref = self.points[-1][1]
            offset_ticks = ref + wrap_ticks(offset_ticks - ref)
        self.points.append((t, offset_ticks, icao))

    def refit(self, now: float) -> None:
        while self.points and self.points[0][0] < now - _SYNC_WINDOW_SECONDS:
            self.points.popleft()
        aircraft = {icao for _t, _y, icao in self.points}
        if len(self.points) < _SYNC_MIN_POINTS or len(aircraft) < _SYNC_MIN_AIRCRAFT:
            self.fitted = False
            self.spread_m = float("inf")
            return
        t_ref = self.points[-1][0]
        xs = [t - t_ref for t, _y, _icao in self.points]
        ys = [y for _t, y, _icao in self.points]
        fit = robust_line(xs, ys)
        if fit is None:
            # All points at one instant: offset only.
            fit = (sum(ys) / len(ys), 0.0)
        self.intercept, self.slope = fit
        self.t_ref = t_ref
        residuals = [y - (self.intercept + self.slope * x) for x, y in zip(xs, ys, strict=True)]
        self.spread_m = 1.4826 * mad(residuals) * METRES_PER_TICK
        self.fitted = True


@dataclass(frozen=True, slots=True)
class TdoaObservation:
    """One transmission heard by several receivers: wall time and raw clocks."""

    time: float
    clocks: dict[str, int]


class ClockSync:
    """All receiver-pair models."""

    def __init__(self) -> None:
        self.pairs: dict[tuple[str, str], PairModel] = {}

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
        names = sorted(name for name in obs.clocks if name in antennas)
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
        names = sorted(name for name in obs.clocks if name in antennas)
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
                out.append(wrap_ticks(raw - expected - model.predict(obs.time)) * METRES_PER_TICK)
        return out

    def range_differences(
        self, obs: TdoaObservation, antennas: dict[str, geodesy.Vec3], max_spread_m: float
    ) -> tuple[str, list[tuple[str, float]]] | None:
        """``(reference, [(receiver, r_receiver - r_reference)])`` in metres."""
        names = sorted(name for name in obs.clocks if name in antennas)
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
