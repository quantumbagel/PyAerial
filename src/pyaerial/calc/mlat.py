"""
Multi-Receiver Signal Fusion & Multilateration (MLAT) for PyAerial.

Provides timestamp synchronization, signal weighting, and Time Difference
of Arrival (TDOA) position estimation when multiple ground receivers observe
the same aircraft transmission.
"""
from __future__ import annotations

import math
import logging
from typing import Sequence

log = logging.getLogger("pyaerial.calc.mlat")

SPEED_OF_LIGHT = 299_792_458.0  # m/s
METERS_PER_DEG_LAT = 111_000.0


def fuse_receiver_telemetry(reports: list[dict]) -> dict:
    """
    Fuse multiple receiver reports for the same packet burst.
    `reports` is a list of dicts: [{"receiver": name, "timestamp": ts, "rssi": level, ...}]
    Returns a weighted merged report.
    """
    if not reports:
        return {}

    if len(reports) == 1:
        return reports[0]

    # Weighted timestamp and signal level based on RSSI / Signal-to-Noise Ratio
    total_weight = 0.0
    weighted_ts = 0.0
    best_report = reports[0]
    max_rssi = -999.0

    for rep in reports:
        rssi = float(rep.get("rssi", rep.get("signal", 1.0)))
        weight = max(rssi + 100.0, 1.0)
        total_weight += weight
        weighted_ts += rep.get("timestamp", 0.0) * weight
        if rssi > max_rssi:
            max_rssi = rssi
            best_report = rep

    fused = dict(best_report)
    if total_weight > 0:
        fused["timestamp"] = weighted_ts / total_weight
    fused["fused_receivers"] = len(reports)
    return fused


def estimate_position_tdoa(receiver_coords: list[tuple[float, float]],
                           timestamps: list[float]) -> tuple[float, float] | None:
    """
    Linearized TDOA position estimation given receiver coordinates [lat, lon]
    and arrival timestamps (seconds). Requires at least 3 receivers.
    """
    if len(receiver_coords) < 3 or len(receiver_coords) != len(timestamps):
        return None

    # Reference receiver (first receiver)
    r0_lat, r0_lon = receiver_coords[0]
    t0 = timestamps[0]

    m_lon0 = max(111_000.0 * math.cos(math.radians(r0_lat)), 1000.0)

    # Convert coordinates to local Cartesian (meters relative to r0)
    rx_pts: list[tuple[float, float]] = []
    d_range: list[float] = []

    for i in range(len(receiver_coords)):
        lat, lon = receiver_coords[i]
        dx = (lon - r0_lon) * m_lon0
        dy = (lat - r0_lat) * METERS_PER_DEG_LAT
        rx_pts.append((dx, dy))

        # Range difference relative to receiver 0
        dt = timestamps[i] - t0
        d_range.append(dt * SPEED_OF_LIGHT)

    # Use first two pairs to solve approximate least-squares position
    sum_x = 0.0
    sum_y = 0.0
    valid_pairs = 0

    for i in range(1, len(rx_pts)):
        xi, yi = rx_pts[i]
        dri = d_range[i]
        if abs(dri) < 1e-6 or (xi == 0 and yi == 0):
            continue

        # Approximate position projection
        r_i = math.hypot(xi, yi)
        proj_scale = (r_i ** 2 - dri ** 2) / (2.0 * max(r_i, 1.0))
        angle = math.atan2(yi, xi)

        sum_x += xi + proj_scale * math.cos(angle)
        sum_y += yi + proj_scale * math.sin(angle)
        valid_pairs += 1

    if valid_pairs == 0:
        return None

    est_dx = sum_x / valid_pairs
    est_dy = sum_y / valid_pairs

    est_lat = r0_lat + (est_dy / METERS_PER_DEG_LAT)
    est_lon = r0_lon + (est_dx / m_lon0)

    return est_lat, est_lon
