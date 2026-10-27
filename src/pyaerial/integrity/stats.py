"""Small robust statistics used by the integrity checks."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation (unscaled)."""
    if not values:
        return 0.0
    centre = statistics.median(values)
    return statistics.median(abs(v - centre) for v in values)


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    """Least-squares ``(intercept, slope)``, or ``None`` when x has no spread."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    return my - slope * mx, slope


def robust_line(
    xs: Sequence[float], ys: Sequence[float], *, passes: int = 2, k: float = 4.0
) -> tuple[float, float] | None:
    """Least squares that drops points beyond ``k`` scaled MADs and refits."""
    fit = linear_fit(xs, ys)
    if fit is None:
        return None
    px, py = list(xs), list(ys)
    for _ in range(passes):
        intercept, slope = fit
        residuals = [y - (intercept + slope * x) for x, y in zip(px, py, strict=True)]
        spread = 1.4826 * mad(residuals)
        if spread <= 0:
            break
        keep = [i for i, r in enumerate(residuals) if abs(r) <= k * spread]
        if len(keep) == len(px) or len(keep) < 2:
            break
        px = [px[i] for i in keep]
        py = [py[i] for i in keep]
        refit = linear_fit(px, py)
        if refit is None:
            break
        fit = refit
    return fit


def circular_median_deg(values: Sequence[float]) -> float | None:
    """Median of angles (degrees) around their circular mean."""
    if not values:
        return None
    s = sum(math.sin(math.radians(v)) for v in values)
    c = sum(math.cos(math.radians(v)) for v in values)
    centre = math.degrees(math.atan2(s, c))
    offsets = [((v - centre + 180.0) % 360.0) - 180.0 for v in values]
    return (centre + statistics.median(offsets)) % 360.0
