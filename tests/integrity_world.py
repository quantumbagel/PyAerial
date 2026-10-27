"""Synthetic sky for integrity tests: receivers, genuine aircraft and a spoofer.

Frames carry only what the monitor reads (DF, address, typecode, CPR parity,
receiver clock, signal level, bearing). Genuine aircraft obey free-space loss
and light-time to each antenna; spoofed ones are radiated from one ground
transmitter while claiming positions elsewhere.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from pyModeS._bits import crc_remainder

from pyaerial.classify import CAT_AIRBORNE_BARO, CAT_IDENT, CAT_VELOCITY
from pyaerial.constants import (
    STORE_ALT,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_PACKET_TYPE,
    STORE_RECV_DATA,
    STORE_VERT_SPEED,
)
from pyaerial.integrity import geodesy
from pyaerial.models import Datum
from pyaerial.receivers.frames import BEAST_CLOCK_HZ, BEAST_CLOCK_MOD, RawFrame

HOME = (35.7275, -78.6959)
# Station constant: rssi_dbfs = K - 20 log10(range_km).
STATION_K = 15.0


def es_position(icao: str, odd: int, tc: int = 11, salt: int = 0) -> str:
    """DF17 airborne position frame (parity not meaningful)."""
    me = (tc << 51) | (odd << 34) | (salt & 0x1FFFF)
    return f"8d{icao.lower()}{me:014x}000000"


def ac13(alt_ft: int) -> int:
    """13-bit Mode S altitude code with Q=1 (25 ft steps)."""
    n = (alt_ft + 1000) // 25
    high = n >> 5  # 6 bits
    b1 = (n >> 4) & 1
    low = n & 0xF
    return (high << 7) | (0 << 6) | (b1 << 5) | (1 << 4) | low


def df4(icao: str, alt_ft: int) -> str:
    """DF4 surveillance altitude reply addressed to *icao*."""
    head = (4 << 27) | ac13(alt_ft)
    body = head << 24
    parity = crc_remainder(body, 56) ^ int(icao, 16)
    return f"{(body | parity):014x}"


@dataclass
class Antenna:
    name: str
    lat: float
    lon: float
    alt: float = 100.0
    clock_offset: float = 0.0  # seconds
    drift_ppm: float = 0.0


@dataclass
class Aircraft:
    icao: str
    lat: float
    lon: float
    alt: float
    speed_kph: float
    track: float
    vrate: float = 0.0
    # Transmitter location when it differs from the claimed one (spoofed).
    emitter: tuple[float, float, float] | None = None
    period: float | None = None  # fixed squitter period; None = 0.4-0.6 s random
    odd: int = 0
    next_tx: float = 0.0
    salt: int = field(default_factory=lambda: random.randrange(1 << 17))

    def claimed(self) -> tuple[float, float, float]:
        return self.lat, self.lon, self.alt

    def source(self) -> tuple[float, float, float]:
        return self.emitter or self.claimed()


def new_plane(icao: str, t: float) -> dict:
    return {
        STORE_INFO: {STORE_ICAO: icao},
        STORE_RECV_DATA: {},
        "calculated_data": {},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: t,
            STORE_MOST_RECENT_PACKET: t,
            STORE_PACKET_TYPE: {},
        },
    }


def _append(plane: dict, field_name: str, value: float, t: float) -> None:
    plane[STORE_RECV_DATA].setdefault(field_name, []).append(Datum(value, t))


class World:
    def __init__(self, antennas: list[Antenna], seed: int = 7):
        self.antennas = antennas
        self.aircraft: list[Aircraft] = []
        self.planes: dict[str, dict] = {}
        self.rng = random.Random(seed)
        self.t = 1_000_000.0

    def add(self, aircraft: Aircraft) -> Aircraft:
        aircraft.next_tx = self.t + self.rng.uniform(0, 0.5)
        self.aircraft.append(aircraft)
        self.planes[aircraft.icao.upper()] = new_plane(aircraft.icao.upper(), self.t)
        return aircraft

    def add_genuine(self, count: int, *, min_km: float = 30, max_km: float = 180) -> None:
        for i in range(count):
            bearing = self.rng.uniform(0, 360)
            dist = self.rng.uniform(min_km, max_km)
            lat, lon = geodesy.destination(HOME[0], HOME[1], bearing, dist * 1000)
            self.add(
                Aircraft(
                    icao=f"a1{i:04x}",
                    lat=lat,
                    lon=lon,
                    alt=self.rng.uniform(3000, 11000),
                    speed_kph=self.rng.uniform(400, 850),
                    track=self.rng.uniform(0, 360),
                )
            )

    def clock(self, antenna: Antenna, t_arrival: float) -> int:
        local = (t_arrival + antenna.clock_offset) * (1 + antenna.drift_ppm * 1e-6)
        return int(round(local * BEAST_CLOCK_HZ)) % BEAST_CLOCK_MOD

    def step(self, dt: float = 0.5) -> list[RawFrame]:
        """Advance *dt* seconds; return the frames every antenna heard."""
        end = self.t + dt
        frames: list[RawFrame] = []
        for ac in self.aircraft:
            while ac.next_tx < end:
                tx = ac.next_tx
                self._move(ac, tx)
                frames.extend(self._transmit(ac, tx))
                ac.odd ^= 1
                ac.next_tx = tx + (ac.period or self.rng.uniform(0.4, 0.6))
        self.t = end
        return frames

    def _move(self, ac: Aircraft, t: float) -> None:
        plane = self.planes[ac.icao.upper()]
        internal = plane[STORE_INTERNAL]
        last = internal[STORE_MOST_RECENT_PACKET]
        dt = max(0.0, t - last)
        ac.lat, ac.lon = geodesy.destination(ac.lat, ac.lon, ac.track, ac.speed_kph / 3.6 * dt)
        ac.alt += ac.vrate * dt
        internal[STORE_MOST_RECENT_PACKET] = t
        _append(plane, STORE_LAT, ac.lat, t)
        _append(plane, STORE_LONG, ac.lon, t)
        _append(plane, STORE_ALT, ac.alt, t)
        _append(plane, STORE_HORIZ_SPEED, ac.speed_kph, t)
        _append(plane, STORE_HEADING, ac.track, t)
        _append(plane, STORE_VERT_SPEED, ac.vrate, t)
        pkt = internal[STORE_PACKET_TYPE]
        pkt[CAT_AIRBORNE_BARO] = pkt.get(CAT_AIRBORNE_BARO, 0) + 1
        pkt[CAT_VELOCITY] = pkt.get(CAT_VELOCITY, 0) + 1
        if pkt[CAT_AIRBORNE_BARO] % 10 == 1:
            pkt[CAT_IDENT] = pkt.get(CAT_IDENT, 0) + 1

    def _transmit(self, ac: Aircraft, t: float) -> list[RawFrame]:
        src = geodesy.ecef(*ac.source())
        hex_msg = es_position(ac.icao, ac.odd, salt=ac.salt + int(t * 10))
        out = []
        for ant in self.antennas:
            here = geodesy.ecef(ant.lat, ant.lon, ant.alt)
            rng_m = geodesy.slant_range_m(here, src)
            arrival = t + rng_m / geodesy.SPEED_OF_LIGHT
            rssi = STATION_K - 20 * math.log10(rng_m / 1000) + self.rng.gauss(0, 1.5)
            if rssi < -40:
                continue
            slat, slon, _salt = ac.source()
            bearing = geodesy.initial_bearing_deg(ant.lat, ant.lon, slat, slon)
            out.append(
                RawFrame(
                    hex=hex_msg,
                    timestamp=arrival,
                    receiver=ant.name,
                    rssi=min(rssi, -3.5),
                    clock=self.clock(ant, arrival),
                    bearing=(bearing + self.rng.gauss(0, 2)) % 360,
                )
            )
        return out
