"""
Canonical unit conversions between stored SI-ish units and aviation display units.

Stored units (see ``UNITS.md``):

- altitude: metres
- horizontal speed: km/h
- vertical speed: m/s
- heading: degrees true
"""

from __future__ import annotations

# ADS-B native → stored
FT_TO_M = 0.3048
KT_TO_KMH = 1.852
FT_PER_MIN_TO_MPS = 0.00508

# stored → aviation display (inverses of the factors above, matching existing literals)
M_TO_FT = 3.28084
KMH_TO_KT = 0.539957
MPS_TO_FT_PER_MIN = 196.8504
