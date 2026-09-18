from __future__ import annotations

from pyaerial.units import (
    FT_PER_MIN_TO_MPS,
    FT_TO_M,
    KMH_TO_KT,
    KMH_TO_MPS,
    KT_TO_KMH,
    M_TO_FT,
    MPS_TO_FT_PER_MIN,
    MPS_TO_KMH,
)


def test_round_trip_altitude():
    assert abs((1000 * FT_TO_M) * M_TO_FT - 1000) < 0.02


def test_round_trip_speed():
    assert abs((100 * KT_TO_KMH) * KMH_TO_KT - 100) < 0.01


def test_round_trip_mps_kmh():
    assert abs((100 * MPS_TO_KMH) * KMH_TO_MPS - 100) < 1e-9


def test_round_trip_vertical_speed():
    assert abs((64 * FT_PER_MIN_TO_MPS) * MPS_TO_FT_PER_MIN - 64) < 0.01
