#!/usr/bin/env python3
"""Profile the engine's per-tick pipeline under a synthetic multi-receiver load.

Builds a sky of genuine aircraft heard by several receivers (valid CRCs, real
CPR positions, velocity, identification, operational status and Mode S
replies, light-time and per-antenna 12 MHz clocks), then drives
``Engine.tick`` in isolated mode (no Redis, no SQLite, no receiver threads)
on a simulated clock: each tick advances ``time.time()`` by ``1 / hz``.

Reports per-stage time per tick, whether the tick budget holds, and the
cProfile hotspots. Run from the project root::

    .venv/bin/python scripts/profile_engine.py
    .venv/bin/python scripts/profile_engine.py --aircraft 500 --seconds 120
"""

from __future__ import annotations

import argparse
import cProfile
import io
import logging
import math
import pstats
import random
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from pyModeS.position import cprNL  # noqa: E402
from pyModeS.util import crc as pms_crc  # noqa: E402

from helpers import make_config  # noqa: E402
from integrity_world import HOME, STATION_K, Antenna, World, df4  # noqa: E402
from pyaerial.config.schema import (  # noqa: E402
    ReceiverConfig,
    ReceiverPositionConfig,
    TrackingConfig,
)
from pyaerial.integrity import geodesy  # noqa: E402
from pyaerial.receivers.frames import RawFrame  # noqa: E402

_CALLSIGN_CHARS = {c: i + 1 for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
_CALLSIGN_CHARS.update({str(d): 48 + d for d in range(10)})
_CALLSIGN_CHARS[" "] = 32


# --- frame encoders (valid parity) ---------------------------------------------


def _long(df_byte: int, icao: str, me: int) -> str:
    body = f"{df_byte:02x}{icao}{me:014x}000000"
    return f"{int(body, 16) | pms_crc(body):028x}"


def _df11(icao: str) -> str:
    body = f"5d{icao}000000"
    return f"{int(body, 16) | pms_crc(body):014x}"


def airborne_position(icao: str, lat: float, lon: float, alt_m: float, odd: int) -> str:
    nb = 1 << 17
    dlat = 360.0 / (60 - odd)
    yz = math.floor(nb * (lat % dlat) / dlat + 0.5)
    rlat = dlat * (yz / nb + math.floor(lat / dlat))
    ni = cprNL(rlat) - odd
    dlon = 360.0 / ni if ni > 0 else 360.0
    xz = math.floor(nb * (lon % dlon) / dlon + 0.5)
    n = max(0, int(alt_m / 0.3048 + 1000) // 25)
    alt12 = ((n >> 4) << 5) | (1 << 4) | (n & 0xF)
    me = (11 << 51) | (alt12 << 36) | (odd << 34) | ((yz % nb) << 17) | (xz % nb)
    return _long(0x8D, icao, me)


def velocity(icao: str, speed_kph: float, track: float, vrate_mps: float) -> str:
    kts = speed_kph / 1.852
    v_ew = kts * math.sin(math.radians(track))
    v_ns = kts * math.cos(math.radians(track))
    vr = int(round(abs(vrate_mps) / 0.00508 / 64)) + 1
    me = (
        (19 << 51)
        | (1 << 48)
        | ((1 if v_ew < 0 else 0) << 42)
        | ((min(int(round(abs(v_ew))) + 1, 1023)) << 32)
        | ((1 if v_ns < 0 else 0) << 31)
        | ((min(int(round(abs(v_ns))) + 1, 1023)) << 21)
        | (1 << 20)  # baro vertical rate
        | ((1 if vrate_mps < 0 else 0) << 19)
        | (min(vr, 511) << 10)
    )
    return _long(0x8D, icao, me)


def identification(icao: str, callsign: str) -> str:
    chars = 0
    for ch in callsign.ljust(8)[:8]:
        chars = (chars << 6) | _CALLSIGN_CHARS.get(ch, 32)
    me = (4 << 51) | (3 << 48) | chars
    return _long(0x8D, icao, me)


def operational_status(icao: str) -> str:
    return _long(0x8D, icao, (31 << 51) | (2 << 13))


# --- synthetic sky -------------------------------------------------------------------


class LoadWorld(World):
    """integrity_world.World with a realistic mix of extended squitters.

    Per transmission event (every 0.4-0.6 s per aircraft): one position and
    one velocity squitter, plus identification, operational status and Mode S
    replies at their typical lower rates.
    """

    def _transmit(self, ac, t: float) -> list[RawFrame]:
        icao = ac.icao.lower()
        msgs = [
            (0.0, airborne_position(icao, ac.lat, ac.lon, ac.alt, ac.odd)),
            (0.05, velocity(icao, ac.speed_kph, ac.track, ac.vrate)),
        ]
        rng = self.rng
        if rng.random() < 0.1:
            msgs.append((0.1, identification(icao, f"TST{icao[-4:].upper()}")))
        if rng.random() < 0.25:
            msgs.append((0.15, operational_status(icao)))
        if rng.random() < 0.3:
            msgs.append((0.2, _df11(icao)))
        if rng.random() < 0.2:
            msgs.append((0.25, df4(icao, int(ac.alt / 0.3048))))
        src = geodesy.ecef(*ac.source())
        out: list[RawFrame] = []
        for ant in self.antennas:
            here = geodesy.ecef(ant.lat, ant.lon, ant.alt)
            rng_m = geodesy.slant_range_m(here, src)
            rssi = STATION_K - 20 * math.log10(rng_m / 1000) + rng.gauss(0, 1.5)
            if rssi < -40:
                continue
            for offset, hex_msg in msgs:
                arrival = t + offset + rng_m / geodesy.SPEED_OF_LIGHT
                out.append(
                    RawFrame(
                        hex=hex_msg,
                        timestamp=arrival,
                        receiver=ant.name,
                        rssi=min(rssi, -3.5),
                        clock=self.clock(ant, arrival),
                    )
                )
        return out

    def _move(self, ac, t: float) -> None:
        # Ground truth only matters to the integrity tests; just fly.
        dt = max(0.0, t - getattr(ac, "_last_move", t))
        ac._last_move = t
        ac.lat, ac.lon = geodesy.destination(
            ac.lat, ac.lon, ac.track, ac.speed_kph / 3.6 * dt
        )
        ac.alt += ac.vrate * dt


def build(aircraft: int, receivers: int, hz: float, seed: int):
    rng = random.Random(seed)
    antennas = []
    for i in range(receivers):
        bearing = 360.0 * i / receivers
        lat, lon = geodesy.destination(HOME[0], HOME[1], bearing, 25_000.0)
        antennas.append(
            Antenna(
                name=f"rx{i}",
                lat=lat,
                lon=lon,
                clock_offset=rng.uniform(0, 1000),
                drift_ppm=rng.uniform(-1, 1),
            )
        )
    config = make_config(
        tracking=TrackingConfig(hz=hz, status_interval=3600),
        receivers={
            ant.name: ReceiverConfig(
                type="dump1090",
                position=ReceiverPositionConfig(
                    latitude=ant.lat, longitude=ant.lon, altitude=ant.alt
                ),
            )
            for ant in antennas
        },
    )
    world = LoadWorld(antennas, seed=seed)
    world.add_genuine(aircraft)
    for ac in world.aircraft:
        ac.vrate = rng.choice([0.0, 0.0, 5.0, -5.0])
    return config, world


# --- driver --------------------------------------------------------------------------


# "drain" includes the merge-window hold-back (ReceiverSupervisor.drain).
_STAGES = {
    "drain": ("supervisor", "drain"),
    "integrity.observe_frames": ("integrity", "observe_frames"),
    "merge/dedup": ("supervisor", "merge"),
    "raw publish": ("supervisor", "publish_raw"),
    "tracker.ingest": ("tracker", "ingest"),
    "integrity.evaluate": ("integrity", "evaluate"),
    "calculator.calculate_all": ("calculator", "calculate_all"),
    "live_store.write_live_planes": ("live_store", "write_live_planes"),
    "expiry/archive": ("engine", "expire_planes"),
}


def _instrument(engine, timings: dict[str, list[float]]) -> dict[str, float]:
    current: dict[str, float] = defaultdict(float)
    owners = {
        "engine": engine,
        "supervisor": engine.supervisor,
        "integrity": engine.integrity,
        "tracker": engine.tracker,
        "calculator": engine.calculator,
        "live_store": engine.live_store,
    }
    for stage, (owner_name, method) in _STAGES.items():
        owner = owners[owner_name]
        real = getattr(owner, method)

        def timed(*args, _real=real, _stage=stage, **kwargs):
            start = time.perf_counter()
            try:
                return _real(*args, **kwargs)
            finally:
                current[_stage] += time.perf_counter() - start

        setattr(owner, method, timed)
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--aircraft", type=int, default=300)
    parser.add_argument("--receivers", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=60.0, help="simulated time")
    parser.add_argument("--hz", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument(
        "--sort", default="tottime", help="pstats sort key, e.g. tottime, cumulative"
    )
    parser.add_argument("--no-profile", action="store_true", help="timings only")
    args = parser.parse_args()

    import requests

    import pyaerial.enrich.aircraft_db as aircraft_db_mod
    from pyaerial.engine import Engine

    def _offline(*_a, **_k):
        raise requests.ConnectionError("profiling: network disabled")

    # Metadata lookups run on a worker thread and would otherwise hit HexDB.
    aircraft_db_mod.requests.get = _offline  # type: ignore[attr-defined]
    logging.getLogger("pyaerial.aircraft_db").setLevel(logging.ERROR)

    config, world = build(args.aircraft, args.receivers, args.hz, args.seed)
    tick = 1.0 / args.hz
    sim_now = time.time()
    world.t = sim_now
    for ac in world.aircraft:
        ac.next_tx = sim_now + world.rng.uniform(0, 0.5)
    real_time = time.time
    time.time = lambda: sim_now  # one coherent clock for the whole engine

    tmp = tempfile.TemporaryDirectory()
    engine = Engine(
        config, isolated=True, aircraft_db_path=str(Path(tmp.name) / "a.db")
    )
    timings: dict[str, list[float]] = defaultdict(list)
    current = _instrument(engine, timings)
    totals: list[float] = []
    dropped = 0
    frames_in = 0
    profiler = cProfile.Profile() if not args.no_profile else None
    processed = 0
    tracked = 0
    positions = 0
    ticks = int(round(args.seconds * args.hz))
    try:
        for _ in range(ticks):
            frames = world.step(tick)
            sim_now = world.t
            frames.sort(key=lambda f: f.timestamp)
            frames_in += len(frames)
            for frame in frames:
                # The receivers' path: drop-oldest when the bounded queue is full.
                engine.supervisor.enqueue(frame)
            current.clear()
            start = time.perf_counter()
            if profiler is not None:
                profiler.enable()
            processed += engine.tick()
            if profiler is not None:
                profiler.disable()
            totals.append(time.perf_counter() - start)
            for stage in _STAGES:
                timings[stage].append(current.get(stage, 0.0))
        dropped = engine.supervisor.counters()["frames_dropped"]
        tracked = len(engine.tracker.planes)
        positions = sum(
            1 for plane in engine.tracker.planes.values() if plane.received_data.get("latitude")
        )
    finally:
        time.time = real_time
        engine.shutdown()
        tmp.cleanup()

    # Skip the first 10 s: tracks and calibration are still warming up.
    warm = min(len(totals) - 1, int(10 * args.hz))
    steady = totals[warm:]
    print(
        f"{args.aircraft} aircraft x {args.receivers} receivers, "
        f"{args.seconds:.0f} s simulated at {args.hz:g} Hz: "
        f"{frames_in / args.seconds:,.0f} frames/s, "
        f"{processed / args.seconds:,.0f} msgs/s ingested, "
        f"{tracked} planes tracked ({positions} with positions)"
    )
    if dropped:
        print(
            f"WARNING: engine message queue overflowed; {dropped:,} frames "
            f"({dropped / frames_in:.1%}) dropped"
        )
    print(
        f"(cProfile overhead included: {'yes' if profiler else 'no'}; "
        f"steady state = after the first {warm} ticks)\n"
    )
    print(f"{'stage':32} {'mean ms':>9} {'p95 ms':>9} {'max ms':>9} {'share':>7}")
    mean_total = statistics.fmean(steady)
    for stage in _STAGES:
        values = timings[stage][warm:]
        mean = statistics.fmean(values)
        p95 = sorted(values)[int(0.95 * (len(values) - 1))]
        print(
            f"{stage:32} {mean * 1e3:9.1f} {p95 * 1e3:9.1f} "
            f"{max(values) * 1e3:9.1f} {mean / mean_total:7.1%}"
        )
    p95_total = sorted(steady)[int(0.95 * (len(steady) - 1))]
    print(
        f"{'TOTAL tick':32} {mean_total * 1e3:9.1f} {p95_total * 1e3:9.1f} "
        f"{max(steady) * 1e3:9.1f}"
    )
    over = sum(1 for value in steady if value > tick)
    print(
        f"\nBudget {tick * 1e3:.0f} ms/tick: "
        f"{'HOLDS' if over == 0 else f'EXCEEDED on {over}/{len(steady)} ticks'} "
        f"(mean uses {mean_total / tick:.0%})"
    )
    if profiler is not None:
        out = io.StringIO()
        stats = pstats.Stats(profiler, stream=out)
        stats.sort_stats(args.sort).print_stats(args.top)
        print(f"\nTop hotspots by {args.sort}:")
        print(out.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
