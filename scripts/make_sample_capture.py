#!/usr/bin/env python3
"""Generate the synthetic dump1090 capture shipped for offline replay demos.

Writes dump1090 ``--mlat`` AVR lines (``@CLOCKHEX;``: a 48-bit 12 MHz clock
followed by the frame) for a handful of aircraft around the example ``home``
(Raleigh, NC) over about 2.5 minutes. Frames are CRC-valid DF17 airborne
position (CPR, alternating even/odd), velocity and identification squitters.

The traffic is built to pass PyAerial's integrity checks the way real traffic
does: positions agree with the reported speed, track and climb rate; position
squitters are randomly spaced 0.4-0.6 s apart; aircraft at altitude first
appear far from the receiver; distant frames are dropped more often; the
clock includes light time to the receiver. Output is deterministic (seeded).

    .venv/bin/python scripts/make_sample_capture.py
    .venv/bin/python scripts/make_sample_capture.py -o /tmp/adsb.raw --seconds 300
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyModeS.position import cprNL  # noqa: E402
from pyModeS.util import crc as pms_crc  # noqa: E402

from pyaerial.integrity import geodesy  # noqa: E402

HOME = (35.727488, -78.695942)  # examples/replay.yaml home (the receiver)
RDU = (35.8776, -78.7875)
DEFAULT_OUT = ROOT / "src" / "pyaerial" / "examples" / "captures" / "adsb.raw"
CLOCK_HZ = 12_000_000
CLOCK_MOD = 1 << 48
FPM_STEP_MPS = 64 * 0.00508  # vertical rate is encoded in 64 ft/min steps

_CHARS = {c: i + 1 for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
_CHARS.update({str(d): 48 + d for d in range(10)})
_CHARS[" "] = 32


# --- encoders --------------------------------------------------------------------


def _df17(icao: str, me: int) -> str:
    body = f"8d{icao}{me:014x}000000"
    # With zero parity bits the CRC remainder is the parity to transmit.
    return f"{int(body, 16) | pms_crc(body):028x}"


def position(icao: str, lat: float, lon: float, alt_m: float, odd: int) -> str:
    nb = 1 << 17
    dlat = 360.0 / (60 - odd)
    yz = math.floor(nb * (lat % dlat) / dlat + 0.5)
    rlat = dlat * (yz / nb + math.floor(lat / dlat))
    ni = cprNL(rlat) - odd
    dlon = 360.0 / ni if ni > 0 else 360.0
    xz = math.floor(nb * (lon % dlon) / dlon + 0.5)
    n = int(round((alt_m / 0.3048 + 1000) / 25))
    alt12 = ((n >> 4) << 5) | (1 << 4) | (n & 0xF)  # Q=1, 25 ft
    me = (11 << 51) | (alt12 << 36) | (odd << 34) | ((yz % nb) << 17) | (xz % nb)
    return _df17(icao, me)


def velocity(icao: str, speed_kph: float, track: float, vrate_mps: float) -> str:
    kts = speed_kph / 1.852
    v_ew = kts * math.sin(math.radians(track))
    v_ns = kts * math.cos(math.radians(track))
    steps = int(round(abs(vrate_mps) / FPM_STEP_MPS))
    me = (
        (19 << 51)
        | (1 << 48)  # subtype 1: ground speed, subsonic
        | ((1 if v_ew < 0 else 0) << 42)
        | ((int(round(abs(v_ew))) + 1) << 32)
        | ((1 if v_ns < 0 else 0) << 31)
        | ((int(round(abs(v_ns))) + 1) << 21)
        | (1 << 20)  # barometric vertical rate
        | ((1 if vrate_mps < 0 else 0) << 19)
        | ((steps + 1) << 10)
    )
    return _df17(icao, me)


def identification(icao: str, callsign: str, category: int) -> str:
    chars = 0
    for ch in callsign.ljust(8)[:8]:
        chars = (chars << 6) | _CHARS.get(ch, 32)
    return _df17(icao, (4 << 51) | (category << 48) | chars)


# --- traffic ---------------------------------------------------------------------


@dataclass
class Flight:
    icao: str
    callsign: str
    category: int  # TC 4 emitter category: 1 light, 3 large, 5 heavy
    lat: float
    lon: float
    alt_m: float
    speed_kph: float
    track: float
    vrate_mps: float  # a multiple of 64 ft/min, as the velocity message encodes
    start: float = 0.0  # seconds into the capture the flight is first heard
    end: float = math.inf


def _from_home(bearing: float, km: float) -> tuple[float, float]:
    return geodesy.destination(HOME[0], HOME[1], bearing, km * 1000.0)


def flights() -> list[Flight]:
    climb = 32 * FPM_STEP_MPS  # 2048 ft/min
    descend = -16 * FPM_STEP_MPS  # 1024 ft/min
    return [
        # Overflights at cruise, first heard far out (as real ones are).
        Flight("a0b1c2", "AAL1871", 5, *_from_home(268, 110), 10668, 830, 80, 0.0),
        Flight("a3c4d5", "DAL2204", 3, *_from_home(2, 95), 11277, 860, 185, 0.0, 10),
        Flight("ab3c4d", "FDX1108", 5, *_from_home(185, 130), 9449, 780, 8, descend),
        # Departure out of RDU, climbing.
        Flight("a6e7f8", "SWA1422", 3, *RDU, 450, 300, 230, climb, 20),
        # Arrival toward RDU, descending.
        Flight("a9f0a1", "JIA5230", 3, *_from_home(40, 45), 2900, 420, 222, descend),
        # Light jet low over the receiver (home).
        Flight("aa1b2c", "EJA411", 1, *_from_home(135, 8), 1524, 260, 315, 0.0, 0, 140),
    ]


def generate(seconds: float, seed: int) -> list[str]:
    rng = random.Random(seed)
    receiver = geodesy.ecef(HOME[0], HOME[1], 10.0)
    clock0 = rng.randrange(1 << 40, 1 << 44)
    events: list[tuple[float, str]] = []
    for flight in flights():
        lat, lon, alt = flight.lat, flight.lon, flight.alt_m
        moved_to = flight.start
        # Independent random schedules per message type, like a transponder.
        next_pos = flight.start + rng.uniform(0, 0.5)
        next_vel = flight.start + rng.uniform(0, 0.5)
        next_id = flight.start + rng.uniform(0, 5)
        odd = rng.randrange(2)
        end = min(seconds, flight.end)
        while True:
            t = min(next_pos, next_vel, next_id)
            if t >= end:
                break
            dt = t - moved_to
            lat, lon = geodesy.destination(
                lat, lon, flight.track, flight.speed_kph / 3.6 * dt
            )
            alt += flight.vrate_mps * dt
            moved_to = t
            if t == next_pos:
                frame = position(flight.icao, lat, lon, alt, odd)
                odd ^= 1
                next_pos += rng.uniform(0.4, 0.6)
            elif t == next_vel:
                # Reported velocity wobbles by a knot or so and a degree; a
                # frame-for-frame identical report would be de-duplicated.
                frame = velocity(
                    flight.icao,
                    flight.speed_kph + rng.gauss(0, 2.0),
                    flight.track + rng.gauss(0, 0.4),
                    flight.vrate_mps + FPM_STEP_MPS * rng.choice((-1, 0, 0, 1)),
                )
                next_vel += rng.uniform(0.4, 0.6)
            else:
                frame = identification(flight.icao, flight.callsign, flight.category)
                next_id += rng.uniform(4.8, 5.2)
            slant = geodesy.slant_range_m(receiver, geodesy.ecef(lat, lon, alt))
            # Weaker (more distant) signals decode less often.
            if rng.random() < 0.05 + 0.25 * min(slant / 150_000.0, 1.0):
                continue
            events.append((t + slant / geodesy.SPEED_OF_LIGHT, frame))
    events.sort()
    return [
        f"@{(clock0 + round(t * CLOCK_HZ)) % CLOCK_MOD:012X}{frame.upper()};"
        for t, frame in events
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seconds", type=float, default=150.0)
    parser.add_argument("--seed", type=int, default=1090)
    args = parser.parse_args()
    lines = generate(args.seconds, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Synthetic dump1090 AVR capture (@ = 12 MHz clock) for offline demos.\n"
        "# Regenerate: python scripts/make_sample_capture.py\n"
    )
    args.output.write_text(header + "\n".join(lines) + "\n")
    print(
        f"wrote {len(lines)} frames ({args.output.stat().st_size:,} bytes) to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
