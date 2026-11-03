"""End to end: a spoofer among genuine aircraft, heard by four Beast receivers.

The sky comes from tests/integrity_world.py (free-space signal loss and light
time to each antenna, receiver clock offsets and drift; the spoofer is radiated
from one ground transmitter while claiming a position 90 km away), but every
transmission is a real, decodable DF17 frame (CPR position, velocity,
identification) streamed over dump1090 Beast TCP to a real Engine.

Integrity only trusts an aircraft after 30 s of evidence, and receiver clock
sync is fitted from trusted aircraft, so the scenario needs about a minute of
sky. ``time.time`` runs FACTOR times faster than real time (see
e2e_support.FastClock); receivers, tracker, integrity and stores all share
that clock, and the Beast 12 MHz timestamps advance at the same rate.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from e2e_support import (
    LOOPBACK,
    PYAERIAL_ENV,
    BeastFeeder,
    EngineThread,
    FastClock,
    FeedEvent,
    ident_frame,
    install_fake_external_http,
    install_fake_redis,
    position_frame,
    query,
    velocity_frame,
    wait_for,
    write_config,
)
from integrity_world import HOME, Aircraft, Antenna, World
from pyaerial.config import load_config
from pyaerial.engine import Engine
from pyaerial.integrity import geodesy
from pyaerial.store.live import LiveReader

FACTOR = 8.0
SKY_SECONDS = 160.0  # generated; the test stops as soon as verdicts settle
SETTLE_TIMEOUT = 20.0  # real seconds
SPOOFER = "b20001"
ANTENNAS = [
    Antenna("a", *HOME, clock_offset=12.3, drift_ppm=3),
    Antenna(
        "b", *geodesy.destination(*HOME, 80, 40_000), clock_offset=-401.7, drift_ppm=-8
    ),
    Antenna(
        "c", *geodesy.destination(*HOME, 200, 45_000), clock_offset=77.0, drift_ppm=11
    ),
    Antenna("d", *geodesy.destination(*HOME, 330, 35_000), clock_offset=5.5),
]


def _level(rssi: float) -> int:
    """dump1090-fa Beast signal byte for *rssi* dBFS."""
    return max(1, min(255, round(255 * 10 ** (rssi / 20))))


class AdsbWorld(World):
    """integrity_world.World whose transmissions are real ADS-B frames."""

    def _transmit(self, ac: Aircraft, t: float):
        frames = [
            replace(f, hex=position_frame(ac.icao, ac.lat, ac.lon, ac.alt, ac.odd))
            for f in super()._transmit(ac, t)
        ]
        velocity = velocity_frame(ac.icao, ac.speed_kph, ac.track, ac.vrate)
        frames += [replace(f, hex=velocity) for f in super()._transmit(ac, t + 0.1)]
        if ac.odd and self.rng.random() < 0.3:
            ident = ident_frame(ac.icao, f"E2E{ac.icao[-3:].upper()}")
            frames += [replace(f, hex=ident) for f in super()._transmit(ac, t + 0.2)]
        return frames


def _sky() -> tuple[list[FeedEvent], tuple[float, float, float], list[str]]:
    world = AdsbWorld(ANTENNAS, seed=11)
    world.add_genuine(10, min_km=25, max_km=120)
    claim = geodesy.destination(*HOME, 45, 90_000)
    emitter = (*geodesy.destination(*HOME, 200, 8_000), 50.0)
    world.add(
        Aircraft(SPOOFER, *claim, alt=9000, speed_kph=700, track=270, emitter=emitter)
    )
    t0 = world.t
    frames = []
    while world.t - t0 < SKY_SECONDS:
        frames.extend(world.step(0.5))
    events = [
        FeedEvent(
            (f.timestamp - t0) / FACTOR, f.receiver, f.hex, f.clock, _level(f.rssi)
        )
        for f in frames
    ]
    genuine = [ac.icao for ac in world.aircraft if ac.icao != SPOOFER]
    return events, emitter, genuine


@pytest.fixture(scope="module")
def spoof_run(tmp_path_factory):
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield _run_scenario(tmp_path_factory.mktemp("e2e_spoof"), monkeypatch)


def _run_scenario(tmp_path, monkeypatch) -> dict:
    for name in PYAERIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    install_fake_redis(monkeypatch)
    external = install_fake_external_http(monkeypatch)
    events, emitter, genuine = _sky()
    feeder = BeastFeeder([a.name for a in ANTENNAS])
    try:
        config_path = write_config(
            tmp_path / "config.yaml",
            {
                "database": {
                    "path": "spoof.db",
                    "redis_uri": "redis://e2e-fake-redis:6379/0",
                },
                "tracking": {"hz": 20, "remember_planes": 600},
                "home": {"latitude": HOME[0], "longitude": HOME[1]},
                "web": {"beast_port": None},
                "receivers": {
                    a.name: {
                        "type": "dump1090",
                        "host": LOOPBACK,
                        "port": feeder.ports[a.name],
                        "format": "beast",
                        "position": {
                            "latitude": a.lat,
                            "longitude": a.lon,
                            "altitude": a.alt,
                        },
                    }
                    for a in ANTENNAS
                },
            },
        )
        config = load_config(config_path)
        monkeypatch.setattr(time, "time", FastClock(FACTOR))
        engine = Engine(config, aircraft_db_path=str(tmp_path / "aircraft.db"))
        reader = LiveReader(config.database.redis_uri)
        runner = EngineThread(engine).start()
        settled: dict[str, dict] = {}
        try:
            feeder.wait_connected()
            feeder.play(events)

            expected = {SPOOFER: "spoofed", **{icao: "ok" for icao in genuine}}

            def verdicts_settled() -> bool:
                nonlocal settled
                flights = reader.get_flights()
                settled = {
                    f["icao"].lower(): (f.get("integrity") or {}) for f in flights
                }
                if {k: v.get("status") for k, v in settled.items()} != expected:
                    return False
                # ...and each aircraft has sent a sample since: its newest live
                # telemetry point carries the verdict.
                return all(
                    (
                        points := reader.get_telemetry(
                            f["flight_id"], limit=1, newest=True
                        )
                    )
                    and points[-1].get("integrity_status")
                    == expected[f["icao"].lower()]
                    for f in flights
                )

            try:
                wait_for(
                    verdicts_settled, SETTLE_TIMEOUT, interval=0.2, message="verdicts"
                )
            except AssertionError:
                summary = {
                    icao: (
                        doc.get("status"),
                        sorted(
                            name
                            for name, c in (doc.get("checks") or {}).items()
                            if c.get("status") != "pass"
                        ),
                    )
                    for icao, doc in settled.items()
                }
                raise AssertionError(
                    f"integrity verdicts did not settle: {summary}"
                ) from None
        finally:
            runner.stop()
            reader.close()
    finally:
        feeder.close()
    assert not external.blocked
    return {
        "db": tmp_path / "spoof.db",
        "genuine": genuine,
        "emitter": emitter,
        "live": settled,
    }


def _flight_id(db, icao: str) -> str:
    rows = query(db, "SELECT flight_id FROM flights WHERE icao = ?", (icao,))
    assert len(rows) == 1, (icao, len(rows))
    return rows[0]["flight_id"]


def test_spoofer_is_spoofed_and_genuine_aircraft_ok_in_the_archive(spoof_run):
    db = spoof_run["db"]
    expected = {SPOOFER: "spoofed", **{icao: "ok" for icao in spoof_run["genuine"]}}
    assert {r["icao"] for r in query(db, "SELECT icao FROM flights")} == set(expected)
    for icao, status in expected.items():
        flight_id = _flight_id(db, icao)
        (flight,) = query(
            db, "SELECT info FROM flights WHERE flight_id = ?", (flight_id,)
        )
        assert json.loads(flight["info"])["integrity"]["status"] == status, icao

        # Verdict log: every change, ending in the final verdict.
        verdicts = query(
            db,
            "SELECT * FROM integrity WHERE flight_id = ? ORDER BY timestamp",
            (flight_id,),
        )
        assert verdicts and verdicts[-1]["status"] == status, (
            icao,
            [v["status"] for v in verdicts],
        )
        assert all(v["icao"] == icao for v in verdicts)

        # Per-sample labels: the verdict in effect when each sample arrived.
        samples = query(
            db,
            "SELECT timestamp, integrity_status FROM telemetry WHERE flight_id = ? "
            "ORDER BY timestamp",
            (flight_id,),
        )
        assert len(samples) > 20
        for sample in samples:
            effective = [v for v in verdicts if v["timestamp"] <= sample["timestamp"]]
            want = effective[-1]["status"] if effective else None
            assert sample["integrity_status"] == want, (icao, sample["timestamp"])
        assert samples[-1]["integrity_status"] == status


def test_spoofer_verdict_names_the_failing_geometry(spoof_run):
    db = spoof_run["db"]
    flight_id = _flight_id(db, SPOOFER)
    (last,) = query(
        db,
        "SELECT * FROM integrity WHERE flight_id = ? ORDER BY timestamp DESC LIMIT 1",
        (flight_id,),
    )
    flags = json.loads(last["flags"])
    checks = json.loads(last["checks"])
    assert "tdoa" in flags and checks["tdoa"]["status"] == "fail"
    assert last["score"] < 0.5
    # Four receivers: TDOA multilaterates the real transmitter near home.
    (flight,) = query(db, "SELECT info FROM flights WHERE flight_id = ?", (flight_id,))
    emitter = json.loads(flight["info"])["integrity"].get("emitter")
    assert emitter, "no multilaterated emitter"
    elat, elon, _alt = spoof_run["emitter"]
    assert (
        geodesy.ground_distance_km(
            emitter["latitude"], emitter["longitude"], elat, elon
        )
        < 5
    )
    # Genuine aircraft re-checked since the receiver clocks synced pass the
    # same check (final integrity documents; checks rerun every 5 s each).
    genuine_tdoa = {}
    for icao in spoof_run["genuine"]:
        (row,) = query(db, "SELECT info FROM flights WHERE icao = ?", (icao,))
        checks = json.loads(row["info"])["integrity"]["checks"]
        if "tdoa" in checks:
            genuine_tdoa[icao] = checks["tdoa"]["status"]
    assert genuine_tdoa and set(genuine_tdoa.values()) == {"pass"}, genuine_tdoa
