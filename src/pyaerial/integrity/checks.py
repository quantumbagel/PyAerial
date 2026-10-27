"""Individual integrity checks.

Each check returns a :class:`CheckResult` or ``None`` when it has too little
evidence to say anything. ``fail`` means the aircraft's claims contradict the
measurement; ``warn`` is weaker evidence. Thresholds are deliberately loose:
one check failing on a real aircraft (multipath, antenna pattern, a quirky
transponder) only lowers the score; several independent failures are needed
before an aircraft reads as spoofed.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from pyaerial.integrity import geodesy
from pyaerial.integrity.identity import (
    icao_to_n_number,
    is_us_address,
    looks_like_n_number,
)
from pyaerial.integrity.stats import circular_median_deg, linear_fit
from pyaerial.integrity.tdoa import wrap_ticks
from pyaerial.receivers.frames import BEAST_CLOCK_HZ

PASS = "pass"
WARN = "warn"
FAIL = "fail"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: str
    detail: str
    value: float | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"status": self.status, "detail": self.detail}
        if self.value is not None and math.isfinite(self.value):
            out["value"] = round(self.value, 2)
        return out


def worst(results: Iterable[CheckResult | None]) -> CheckResult | None:
    best: CheckResult | None = None
    for result in results:
        if result is None:
            continue
        if best is None or _RANK[result.status] > _RANK[best.status]:
            best = result
    return best


def graded(value: float, fail_at: float, warn_at: float, detail: str) -> CheckResult:
    """``fail`` above *fail_at*, ``warn`` above *warn_at*, else ``pass``."""
    if value > fail_at:
        return CheckResult(FAIL, detail, value)
    if value > warn_at:
        return CheckResult(WARN, detail, value)
    return CheckResult(PASS, detail, value)


# --- Geometry against receiver positions ------------------------------------


def radio_horizon(
    distances: list[tuple[str, float, float]],
    max_range_km: float,
) -> CheckResult | None:
    """*distances*: ``(receiver, ground_km, horizon_km)`` for receivers that heard it."""
    if not distances:
        return None
    name, dist, limit = max(distances, key=lambda item: item[1] / max(item[2], 1e-6))
    if dist > max_range_km:
        return CheckResult(
            FAIL, f"{name} heard it at {dist:.0f} km, beyond {max_range_km:.0f} km", dist
        )
    ratio = dist / limit if limit > 0 else math.inf
    detail = f"{name}: {dist:.0f} km, radio horizon {limit:.0f} km"
    return CheckResult(FAIL if ratio > 1 else PASS, detail, ratio)


def rssi_range(excess_db: list[tuple[str, float]], threshold_db: float) -> CheckResult | None:
    """*excess_db*: ``(receiver, dB stronger than the station model predicts)``."""
    if not excess_db:
        return None
    name, excess = max(excess_db, key=lambda item: item[1])
    detail = f"{name}: signal {excess:+.0f} dB vs. expected for claimed range"
    return graded(excess, threshold_db, threshold_db * 0.7, detail)


def rssi_trend(samples: list[tuple[float, float]]) -> CheckResult | None:
    """*samples*: ``(20*log10(range_km), rssi_dbfs)``.

    Free-space loss makes the signal fall 1 dB per dB of ``20 log10(range)``.
    A fixed transmitter faking a moving target keeps a flat signal however far
    the claimed track goes.
    """
    if len(samples) < 30:
        return None
    xs = [x for x, _y in samples]
    if max(xs) - min(xs) < 8.0:
        return None
    fit = linear_fit(xs, [y for _x, y in samples])
    if fit is None:
        return None
    slope = fit[1]
    detail = f"signal changes {slope:+.2f} dB per dB of path loss (expect about -1)"
    if slope > -0.1:
        return CheckResult(FAIL, detail, slope)
    if slope > -0.4:
        return CheckResult(WARN, detail, slope)
    return CheckResult(PASS, detail, slope)


def aoa(
    bearing_errors: list[float],
    elevation_errors: list[float],
    tolerance_deg: float,
    receiver: str,
) -> CheckResult | None:
    """Median |measured - expected| angle of arrival."""
    if len(bearing_errors) < 5:
        return None
    err = statistics.median(abs(e) for e in bearing_errors)
    result = graded(
        err,
        tolerance_deg,
        tolerance_deg * 0.6,
        f"{receiver}: bearing off by {err:.0f}° from claimed position",
    )
    if len(elevation_errors) >= 5:
        elev = statistics.median(abs(e) for e in elevation_errors)
        if elev > 10.0 and _RANK[result.status] < _RANK[FAIL]:
            return CheckResult(
                FAIL, f"{receiver}: elevation off by {elev:.0f}° from claimed position", elev
            )
    return result


def tdoa(residuals_m: list[float], tolerance_m: float) -> CheckResult | None:
    if len(residuals_m) < 3:
        return None
    err = statistics.median(abs(r) for r in residuals_m)
    return graded(
        err,
        tolerance_m,
        tolerance_m * 0.6,
        f"arrival-time difference misses claimed position by {err / 1000:.1f} km",
    )


# --- Mode S / transponder behaviour -----------------------------------------


def mode_s_altitude(diffs_m: list[float], tolerance_m: float) -> CheckResult | None:
    if len(diffs_m) < 3:
        return None
    err = statistics.median(abs(d) for d in diffs_m)
    return graded(
        err,
        tolerance_m,
        tolerance_m,
        f"Mode S reply altitude differs from ADS-B by {err:.0f} m",
    )


def mode_s_corroboration(corroborated: bool, station_rate: float) -> CheckResult:
    if corroborated:
        return CheckResult(PASS, "Mode S replies seen for this address", station_rate)
    return CheckResult(
        FAIL,
        f"no Mode S replies, though {station_rate:.0%} of other aircraft send them",
        station_rate,
    )


def duplicate_icao(rejects: int, positions: int) -> CheckResult | None:
    if positions < 10 and rejects == 0:
        return None
    detail = f"{rejects} position(s) far from the track for this address"
    if rejects >= 3 and rejects >= 0.05 * max(positions, 1):
        return CheckResult(FAIL, detail, rejects)
    if rejects >= 2:
        return CheckResult(WARN, detail, rejects)
    return CheckResult(PASS, detail, rejects)


def timing(clock_rows: list[tuple[int, int]]) -> CheckResult | None:
    """Airborne position squitter spacing from one receiver's 12 MHz clock.

    Transponders send position at random 0.4-0.6 s intervals, alternating
    even/odd CPR encoding. Generated traffic is often perfectly periodic or
    breaks the alternation.
    """
    if len(clock_rows) < 16:
        return None
    base = clock_rows[0][0]
    rows = sorted(
        (wrap_ticks(clock - base) / BEAST_CLOCK_HZ, odd) for clock, odd in clock_rows
    )
    adjacent: list[float] = []
    same_parity = 0
    for (t0, f0), (t1, f1) in zip(rows, rows[1:], strict=False):
        gap = t1 - t0
        if 0.3 <= gap <= 0.7:
            adjacent.append(gap)
            same_parity += f0 == f1
    if len(adjacent) < 15:
        return None
    spread = statistics.pstdev(adjacent)
    outside = sum(1 for gap in adjacent if not 0.38 <= gap <= 0.62) / len(adjacent)
    parity = same_parity / len(adjacent)
    detail = (
        f"interval spread {spread * 1000:.0f} ms (expect ~58), "
        f"{outside:.0%} outside 0.4-0.6 s, {parity:.0%} even/odd repeats"
    )
    if spread < 0.012 or outside > 0.3 or parity > 0.5:
        return CheckResult(FAIL, detail, spread)
    if spread < 0.025 or outside > 0.15 or parity > 0.15:
        return CheckResult(WARN, detail, spread)
    return CheckResult(PASS, detail, spread)


def replay(repeats: int, moving: bool) -> CheckResult | None:
    if not moving:
        return None
    detail = f"{repeats} identical position frame(s) repeated within 5 s"
    if repeats >= 3:
        return CheckResult(FAIL, detail, repeats)
    if repeats >= 1:
        return CheckResult(WARN, detail, repeats)
    return CheckResult(PASS, "no repeated position frames", 0)


def message_mix(positions: int, idents: int, velocities: int) -> CheckResult | None:
    """Real airborne transponders send velocity as often as position, ident 1/10."""
    if positions < 20:
        return None
    if velocities == 0:
        return CheckResult(FAIL, f"{positions} positions but no velocity messages", 0)
    if positions >= 60 and idents == 0:
        return CheckResult(FAIL, f"{positions} positions but no identification", 0)
    ratio = velocities / positions
    if ratio < 0.1:
        return CheckResult(WARN, f"velocity/position message ratio {ratio:.2f}", ratio)
    return CheckResult(PASS, f"velocity/position message ratio {ratio:.2f}", ratio)


def appearance(first_km: float, altitude_m: float, typical_km: float) -> CheckResult:
    """Aircraft at altitude normally enter coverage near its edge."""
    limit = max(20.0, 0.2 * typical_km)
    detail = f"first seen {first_km:.0f} km away at {altitude_m:.0f} m"
    if altitude_m > 3000 and first_km < limit:
        return CheckResult(FAIL, detail + " (appeared mid-air)", first_km)
    return CheckResult(PASS, detail, first_km)


def identity(icao: str, callsign: str | None, registration: str | None) -> CheckResult | None:
    expected = icao_to_n_number(icao)
    us = is_us_address(icao)
    for label, raw in (("callsign", callsign), ("registration", registration)):
        if not looks_like_n_number(raw):
            continue
        text = raw.strip().upper()
        if not us:
            return CheckResult(FAIL, f"{label} {text} is US but address {icao} is not")
        if expected != text:
            return CheckResult(
                FAIL, f"{label} {text} but address {icao} belongs to {expected}"
            )
        return CheckResult(PASS, f"{label} {text} matches address {icao}")
    return None


# --- Kinematics ---------------------------------------------------------------


def kinematics(
    *,
    elapsed_s: float,
    distance_m: float,
    displacement_bearing: float | None,
    reported_speed_kph: float | None,
    reported_tracks: list[float],
    climb_mps: float | None,
    reported_vrate_mps: float | None,
) -> CheckResult | None:
    """Position-derived motion against the velocity the aircraft reports."""
    if elapsed_s <= 0:
        return None
    results: list[CheckResult] = []
    derived_kph = distance_m / elapsed_s * 3.6
    if reported_speed_kph is not None and max(reported_speed_kph, derived_kph) >= 50:
        error = abs(derived_kph - reported_speed_kph)
        limit = max(0.3 * reported_speed_kph, 80.0)
        results.append(
            graded(
                error / limit,
                1.0,
                0.5,
                f"moved at {derived_kph:.0f} km/h, reports {reported_speed_kph:.0f} km/h",
            )
        )
    if (
        displacement_bearing is not None
        and reported_tracks
        and (reported_speed_kph or 0) > 150
        and distance_m > 1000
    ):
        track = circular_median_deg(reported_tracks)
        if track is not None:
            off = abs(geodesy.angle_diff_deg(displacement_bearing, track))
            results.append(
                graded(off, 45.0, 25.0, f"moved toward {displacement_bearing:.0f}°, reports track {track:.0f}°")
            )
    if climb_mps is not None and reported_vrate_mps is not None:
        error = abs(climb_mps - reported_vrate_mps)
        results.append(
            graded(
                error,
                12.0,
                7.0,
                f"altitude changes {climb_mps:+.1f} m/s, reports {reported_vrate_mps:+.1f} m/s",
            )
        )
    if derived_kph > 1500:
        results.append(CheckResult(WARN, f"moving at {derived_kph:.0f} km/h", derived_kph))
    return worst(results)


def cross_network(distance_km: float | None, found: bool, max_km: float, long_tracked: bool) -> CheckResult | None:
    if found and distance_km is not None:
        return graded(
            distance_km,
            max_km,
            max_km,
            f"public network places it {distance_km:.1f} km from here",
        )
    if not found and long_tracked:
        return CheckResult(WARN, "public network has no position for this address")
    return None


def emitter_cluster(reason: str, size: int) -> CheckResult:
    return CheckResult(FAIL, f"{reason} shared by {size} aircraft (one transmitter)", size)
