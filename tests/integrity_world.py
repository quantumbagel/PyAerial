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

from pyModeS.util import crc

from helpers import make_config
from helpers import with_crc as with_crc  # re-exported for the integrity tests
from pyaerial.classify import CAT_AIRBORNE_BARO, CAT_IDENT, CAT_VELOCITY
from pyaerial.config.schema import (
    IntegrityConfig,
    ReceiverConfig,
    ReceiverPositionConfig,
)
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
from pyaerial.integrity import IntegrityMonitor, geodesy
from pyaerial.models import Datum
from pyaerial.receivers.frames import BEAST_CLOCK_HZ, BEAST_CLOCK_MOD, RawFrame
from pyaerial.receivers.positions import ReceiverPositions

HOME = (35.7275, -78.6959)
# Station constant: rssi_dbfs = K - 20 log10(range_km).
STATION_K = 15.0


def es_position(icao: str, odd: int, tc: int = 11, salt: int = 0) -> str:
    """DF17 airborne position frame with valid parity (CPR not meaningful)."""
    me = (tc << 51) | (odd << 34) | (salt & 0x1FFFF)
    return with_crc(f"8d{icao.lower()}{me:014x}000000")


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
    parity = crc(f"{body:014x}") ^ int(icao, 16)
    return f"{(body | parity):014x}"


@dataclass
class Antenna:
    name: str
    lat: float
    lon: float
    alt: float = 100.0
    clock_offset: float = 0.0  # seconds
    drift_ppm: float = 0.0
    # AoA array orientation error: degrees added to every measured bearing.
    bearing_error: float = 0.0


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
                    bearing=(bearing + ant.bearing_error + self.rng.gauss(0, 2)) % 360,
                )
            )
        return out


# --- monitor helpers shared by the integrity tests ---------------------------


def integrity_config(antennas: list[Antenna], *, kind: str = "dump1090", **integrity):
    receivers = {
        a.name: ReceiverConfig(
            type=kind,
            port=1234 if kind == "aoa" else None,
            position=ReceiverPositionConfig(latitude=a.lat, longitude=a.lon, altitude=a.alt),
        )
        for a in antennas
    }
    config = make_config(receivers=receivers)
    if integrity:
        config.integrity = IntegrityConfig(**integrity)
    return config


def make_monitor(world: World, config) -> IntegrityMonitor:
    positions = ReceiverPositions(config, fetch=False)
    # Started long ago so the appearance check is not in its startup grace.
    return IntegrityMonitor(config, positions, started_at=world.t - 1000)


def run_world(world: World, monitor: IntegrityMonitor, seconds: float, extra=None) -> None:
    steps = int(seconds / 0.5)
    for _ in range(steps):
        frames = world.step(0.5)
        if extra is not None:
            frames.extend(extra(world))
        monitor.observe_frames(frames)
        monitor.evaluate(world.planes, world.t, force=True)


def integrity_of(world: World, icao: str) -> dict:
    return world.planes[icao.upper()][STORE_INFO]["integrity"]


def add_spoofer(world: World, icao: str, emitter_km: float = 5.0, claim_km: float = 150.0) -> Aircraft:
    """A target claimed far away but radiated from a transmitter near home."""
    lat, lon = geodesy.destination(HOME[0], HOME[1], 45, claim_km * 1000)
    elat, elon = geodesy.destination(HOME[0], HOME[1], 200, emitter_km * 1000)
    return world.add(
        Aircraft(
            icao=icao,
            lat=lat,
            lon=lon,
            alt=9000,
            speed_kph=700,
            track=270,
            emitter=(elat, elon, 50.0),
        )
    )


def with_velocity(frames: list[RawFrame]) -> list[RawFrame]:
    """Add two non-position squitters per position (~6 DF17/s, as real)."""
    out = list(frames)
    for f in frames:
        for k in (1, 2):
            me = (19 << 51) | ((int(f.hex[14:22], 16) * 3 + k) & 0xFFFFFFFFFFF)
            out.append(
                RawFrame(
                    with_crc(f"8d{f.hex[2:8]}{me:014x}000000"),
                    f.timestamp + 0.15 * k,
                    f.receiver,
                    f.rssi,
                    (f.clock + int(0.15 * k * BEAST_CLOCK_HZ)) % BEAST_CLOCK_MOD,
                )
            )
    return out


def drifting_receivers() -> list[Antenna]:
    """Four receivers with free-running clocks, three of them drifting."""
    return [
        Antenna("a", *HOME, clock_offset=12.3, drift_ppm=3),
        Antenna(
            "b",
            *geodesy.destination(*HOME, 80, 40_000),
            clock_offset=-401.7,
            drift_ppm=-8,
        ),
        Antenna(
            "c",
            *geodesy.destination(*HOME, 200, 45_000),
            clock_offset=77.0,
            drift_ppm=11,
        ),
        Antenna("d", *geodesy.destination(*HOME, 330, 35_000), clock_offset=5.5),
    ]
