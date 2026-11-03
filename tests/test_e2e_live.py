"""End to end: dump1090 Beast TCP -> Engine -> alerts/Redis/SQLite -> portal.

One scenario runs once per module (``live`` fixture) and the tests below
assert on what every output saw:

* two Beast feeders (receivers ``roof`` and ``mast``) stream valid DF17
  position / velocity / identification frames for two aircraft. Clock,
  signal and ICAO bytes contain 0x1a, so the stream exercises Beast escaping.
  ``roof`` drops its TCP connection mid-stream and the engine reconnects.
* aircraft A flies low inside zone ``pad`` (rule ``low_pass``) and then
  climbs out of the rule; aircraft B cruises far away at altitude.
* a real ``Engine`` built from a YAML config runs ``run()`` in a thread with a
  tmp SQLite archive (``archive.raw_frames: true``) and a webhook alerter
  pointed at a local HTTP server; it is stopped through its own shutdown path.
* the real portal (``create_app`` via ``connect_stores``, as ``pyaerial web``
  does) is served by uvicorn on 127.0.0.1 and read over HTTP, ``/ws/live``,
  ``/ws/beast`` and the Beast TCP re-serve port while the engine runs.

Only Redis (tests/fake_redis.py, made thread safe and given pub/sub) and the
public aircraft APIs are faked.
"""

from __future__ import annotations

import contextlib
import json
import random
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from e2e_support import (
    LOOPBACK,
    PYAERIAL_ENV,
    BeastFeeder,
    EngineThread,
    FeedEvent,
    HttpSink,
    PortalServer,
    TcpCollector,
    WsCollector,
    count,
    expected_speed_kmh,
    ident_frame,
    install_fake_external_http,
    install_fake_redis,
    position_frame,
    quantized_alt_ft,
    query,
    strict_beast_decode,
    velocity_frame,
    wait_for,
    write_config,
    ws_rpc,
)
from pyaerial.api.app import create_app
from pyaerial.api.connect import connect_stores
from pyaerial.config import load_config
from pyaerial.engine import Engine
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.integrity import geodesy
from pyaerial.receivers.frames import BeastParser, es_crc_ok

HOME = (35.7275, -78.6959)
ROOF = (35.7275, -78.6959, 100.0)
MAST = (35.7400, -78.6900)  # served by the receiver.json endpoint only
ZONE = [
    [35.690, -78.715],
    [35.710, -78.715],
    [35.710, -78.680],
    [35.690, -78.680],
    [35.690, -78.715],
]
SCRIPT_SECONDS = 6.0
DISCONNECT_AT = 2.5
CLIMB_AT = 4.6
FT_TO_M = 0.3048
# Clocks whose upper bytes are 0x1a: every frame carries escaped bytes.
ROOF_CLOCK0 = 0x1A1A_0000_0000
MAST_CLOCK0 = 0x0042_1A00_0000
SIGNAL_ESC = 0x1A  # also an escaped byte (-19.8 dBFS)


@dataclass(frozen=True)
class Track:
    icao: str
    callsign: str
    lat: float
    lon: float
    speed_kmh: float
    heading: float
    low_alt: float
    high_alt: float | None = None  # altitude after CLIMB_AT

    def position(self, t: float) -> tuple[float, float]:
        return geodesy.destination(
            self.lat, self.lon, self.heading, self.speed_kmh / 3.6 * t
        )

    def altitude(self, t: float) -> float:
        if self.high_alt is not None and t >= CLIMB_AT:
            return self.high_alt
        return self.low_alt


A = Track("4b1a1a", "E2EA01", 35.700, -78.700, 300.0, 90.0, 600.0, 1500.0)
B = Track(
    "c0ffee", "E2EB02", *geodesy.destination(*HOME, 45, 25_000), 800.0, 45.0, 9000.0
)
C = Track(
    "a0e2e3", "E2EC03", *geodesy.destination(*HOME, 270, 30_000), 600.0, 270.0, 7000.0
)
# receiver -> (signal byte, clock origin) per aircraft. roof is the stronger for B.
HEARD_BY = {
    A.icao: [("roof", SIGNAL_ESC, ROOF_CLOCK0)],
    B.icao: [("roof", 0x40, ROOF_CLOCK0), ("mast", 0x20, MAST_CLOCK0)],
    C.icao: [("roof", 0x50, ROOF_CLOCK0)],
}
HEXDB = {
    A.icao: {
        "Registration": "HB-JZA",
        "Manufacturer": "Airbus",
        "Type": "A320 214",
        "RegisteredOwners": "E2E Air",
        "Registered": "Switzerland",
        "ICAOTypeCode": "A320",
        "OperatorFlagCode": "EZS",
    }
}


@dataclass(frozen=True)
class Truth:
    icao: str
    t: float
    lat: float
    lon: float
    alt_m: float  # as the 25 ft altitude code carries it


def build_script(
    tracks: list[Track], seconds: float, seed: int
) -> tuple[list[FeedEvent], dict[str, Truth]]:
    """Frames each receiver hears, plus the true position behind each position hex."""
    rng = random.Random(seed)
    events: list[FeedEvent] = []
    truth: dict[str, Truth] = {}

    def emit(at: float, track: Track, hex_msg: str) -> None:
        for receiver, level, clock0 in HEARD_BY[track.icao]:
            events.append(
                FeedEvent(at, receiver, hex_msg, clock0 + int(at * 12e6), level)
            )

    for track in tracks:
        emit(0.02, track, ident_frame(track.icao, track.callsign))
        t = rng.uniform(0.05, 0.3)
        odd = 0
        n = 0
        while t < seconds:
            lat, lon = track.position(t)
            alt = track.altitude(t)
            hex_msg = position_frame(track.icao, lat, lon, alt, odd)
            truth[hex_msg] = Truth(
                track.icao, t, lat, lon, quantized_alt_ft(alt) * FT_TO_M
            )
            emit(t, track, hex_msg)
            if t + 0.1 < seconds:
                emit(
                    t + 0.1,
                    track,
                    velocity_frame(track.icao, track.speed_kmh, track.heading),
                )
            if n % 4 == 3 and t + 0.2 < seconds:
                emit(t + 0.2, track, ident_frame(track.icao, track.callsign))
            odd ^= 1
            n += 1
            # Position squitters are randomly spaced 0.4-0.6 s (DO-260B).
            t += rng.uniform(0.4, 0.6)
    return events, truth


@dataclass
class LiveScenario:
    tmp: Path
    config_path: Path
    db: Path
    engine: Engine
    feeder: BeastFeeder
    sink: HttpSink
    portal: PortalServer
    live_ws: WsCollector
    beast_ws: WsCollector
    beast_tcp: TcpCollector
    events: list[FeedEvent]
    truth: dict[str, Truth]
    play_started: float = 0.0
    shutdown_started: float = 0.0
    shutdown_seconds: float = 0.0
    ready_running: dict = field(default_factory=dict)
    ready_stopped: dict = field(default_factory=dict)
    beast_tcp_bytes: bytes = b""
    beast_ws_bytes: bytes = b""
    live_messages: list[tuple[float, Any]] = field(default_factory=list)
    live_telemetry_rpc: dict = field(default_factory=dict)
    external: Any = None
    redis: Any = None

    def flight_id(self, icao: str) -> str:
        rows = query(self.db, "SELECT flight_id FROM flights WHERE icao = ?", (icao,))
        assert len(rows) == 1, (
            f"expected one archived flight for {icao}, got {len(rows)}"
        )
        return rows[0]["flight_id"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def _config(tmp: Path, feeder: BeastFeeder, sink: HttpSink) -> dict:
    hook = {"method": "webhook", "options": {"url": "${E2E_WEBHOOK_URL}"}}
    return {
        "database": {
            "path": "archive.db",
            "redis_uri": "redis://e2e-fake-redis:6379/0",
        },
        "tracking": {"hz": 10, "remember_planes": 120, "status_interval": 60},
        "archive": {"flights": "all", "min_positions": 2, "raw_frames": True},
        "logging": {"level": "info"},
        "home": {"latitude": HOME[0], "longitude": HOME[1]},
        # beast_port 0 is invalid config (it would bind a random port); pick a
        # free one here instead.
        "web": {"beast_host": LOOPBACK, "beast_port": _free_port()},
        "receivers": {
            "roof": {
                "type": "dump1090",
                "host": LOOPBACK,
                "port": feeder.ports["roof"],
                "format": "beast",
                "position": {
                    "latitude": ROOF[0],
                    "longitude": ROOF[1],
                    "altitude": ROOF[2],
                },
            },
            "mast": {
                "type": "dump1090",
                "host": LOOPBACK,
                "port": feeder.ports["mast"],
                "position_url": f"{sink.url}/data/receiver.json",
            },
        },
        "zones": {
            "pad": {
                "coordinates": ZONE,
                "rules": [
                    {
                        "name": "low_pass",
                        "when": {"altitude": {"max": 1000}, "eta": {"max": 30}},
                        "dwell_seconds": 1,
                        "on_activate": [hook],
                        "on_deactivate": [hook],
                    }
                ],
            }
        },
    }


def _flights_messages(messages: list[Any]) -> list[dict]:
    return [m for m in messages if isinstance(m, dict) and m.get("type") == "flights"]


def _live_flight(message: dict, icao: str) -> dict | None:
    for flight in message.get("flights", []):
        if str(flight.get("icao", "")).lower() == icao:
            return flight
    return None


def _ready(portal: PortalServer) -> dict:
    response = httpx.get(f"{portal.http}/ready", timeout=5)
    body = response.json()
    body["_status_code"] = response.status_code
    return body


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("e2e_live")
    closers: list[Any] = []
    try:
        for name in PYAERIAL_ENV:
            mp.delenv(name, raising=False)
        redis_server = install_fake_redis(mp)
        external = install_fake_external_http(mp, hexdb=HEXDB)
        sink = HttpSink(receiver_json={"lat": MAST[0], "lon": MAST[1]})
        closers.append(sink.close)
        mp.setenv("E2E_WEBHOOK_URL", f"{sink.url}/hook")
        feeder = BeastFeeder(["roof", "mast"])
        closers.append(feeder.close)

        config_path = write_config(tmp / "config.yaml", _config(tmp, feeder, sink))
        config = load_config(config_path)
        engine = Engine(config, aircraft_db_path=str(tmp / "aircraft.db"))

        # The portal exactly as `pyaerial web -c config.yaml` builds it.
        web_config, history, live_store = connect_stores(str(config_path))
        portal_db = AircraftDB(tmp / "portal_aircraft.db")
        closers.extend([portal_db.close, live_store.close, history.close])
        portal = PortalServer(
            create_app(
                config=web_config,
                history=history,
                live_store=live_store,
                aircraft_db=portal_db,
            )
        ).start()
        closers.append(portal.stop)
        api = httpx.get(f"{portal.http}/api", timeout=5).json()
        beast_port = api["beast"]["tcp_port"]
        assert beast_port, api["beast"]

        live_ws = WsCollector(f"{portal.ws}/ws/live")
        beast_ws = WsCollector(f"{portal.ws}/ws/beast", binary=True)
        beast_tcp = TcpCollector(LOOPBACK, beast_port)
        closers.extend([live_ws.close, beast_ws.close, beast_tcp.close])
        live_ws.wait(
            lambda m: any(x.get("type") == "stats" for x in m), 10, "ws snapshot"
        )

        events, truth = build_script([A, B], SCRIPT_SECONDS, seed=20261103)
        scenario = LiveScenario(
            tmp=tmp,
            config_path=config_path,
            db=Path(config.database.path),
            engine=engine,
            feeder=feeder,
            sink=sink,
            portal=portal,
            live_ws=live_ws,
            beast_ws=beast_ws,
            beast_tcp=beast_tcp,
            events=events,
            truth=truth,
            external=external,
            redis=redis_server,
        )

        runner = EngineThread(engine).start()
        try:
            feeder.wait_connected(count=1)
            scenario.ready_running = wait_for(
                lambda: (body := _ready(portal))["engine"] and body,
                10,
                message="/ready to report a live engine",
            )
            scenario.play_started = time.monotonic()
            player = feeder.play(events, disconnect={"roof": DISCONNECT_AT})
            player.join(SCRIPT_SECONDS + 10)
            assert not player.is_alive()
            # Every frame that went out is processed and archived raw...
            wait_for(
                lambda: (
                    count(scenario.db, "SELECT COUNT(*) FROM frames")
                    >= len(feeder.sent)
                ),
                10,
                message="raw frame archive to catch up",
            )
            # ...and the portal has pushed the aircraft's final positions.
            last_a = max(
                (tr for tr in truth.values() if tr.icao == A.icao), key=lambda tr: tr.t
            )
            live_ws.wait(
                lambda m: any(
                    (f := _live_flight(msg, A.icao))
                    and f.get("latitude") is not None
                    and abs(f["latitude"] - last_a.lat) < 1e-4
                    and abs(f["longitude"] - last_a.lon) < 1e-4
                    for msg in _flights_messages(m)
                ),
                10,
                "portal flights push with A's last position",
            )
            scenario.live_telemetry_rpc = ws_rpc(
                portal.ws,
                "fetchTelemetry",
                {
                    "flightId": next(
                        f["flight_id"]
                        for f in live_store.get_flights()
                        if f["icao"].lower() == B.icao
                    ),
                    "view": "live",
                },
            )
        finally:
            scenario.shutdown_started = time.monotonic()
            runner.stop()
            scenario.shutdown_seconds = time.monotonic() - scenario.shutdown_started

        # The portal notices the engine is gone and the live flights cleared.
        live_ws.wait(
            lambda m: any(
                x.get("type") == "stats" and x["stats"].get("engine_live") is False
                for x in m
            ),
            15,
            "stats push with engine_live false",
        )
        scenario.ready_stopped = _ready(portal)
        time.sleep(0.3)  # let the Beast feeds flush anything still queued
        scenario.beast_tcp_bytes = beast_tcp.received()
        scenario.beast_ws_bytes = b"".join(beast_ws.snapshot())
        scenario.live_messages = live_ws.timed()
        yield scenario
    finally:
        for close in reversed(closers):
            with contextlib.suppress(Exception):  # best-effort teardown
                close()
        mp.undo()


# --- 1. live TCP ingest ----------------------------------------------------------


def test_receivers_reconnect_after_mid_stream_disconnect(live):
    assert live.feeder.disconnects.get("roof") == 1
    # The engine came back on its own (receiver restart with back-off).
    assert live.feeder.accepts["roof"] >= 2
    assert live.feeder.accepts["mast"] == 1
    roof_frames = [s for s in live.feeder.sent if s.receiver == "roof"]
    assert {s.connection for s in roof_frames} >= {1, 2}, (
        "no roof frames after reconnect"
    )
    # A's frames during the outage are lost (a feeder does not buffer).
    assert any(e.receiver == "roof" for e in live.feeder.dropped)
    # The disconnect did not split A or B into two flights.
    for icao in (A.icao, B.icao):
        live.flight_id(icao)


def test_aircraft_tracked_with_decoded_position_altitude_callsign(live):
    """The portal's live flights (from the engine via Redis) match the truth."""
    flights = _flights_messages([m for _t, m in live.live_messages])
    assert flights, "no flights pushes"
    for track in (A, B):
        docs = [f for msg in flights if (f := _live_flight(msg, track.icao))]
        assert docs, f"{track.icao} never appeared in a flights push"
        assert {d["callsign"] for d in docs if d.get("callsign")} == {track.callsign}
        assert all(d["is_live"] for d in docs)
        # The newest position the portal showed is the last frame's position.
        last = max(
            (tr for tr in live.truth.values() if tr.icao == track.icao),
            key=lambda tr: tr.t,
        )
        newest = next(
            d
            for d in reversed(docs)
            if d.get("latitude") is not None
            and abs(d["latitude"] - last.lat) < 1e-4
            and abs(d["longitude"] - last.lon) < 1e-4
        )
        assert newest["altitude"] == pytest.approx(last.alt_m, abs=0.5)
        assert newest["speed"] == pytest.approx(
            expected_speed_kmh(track.speed_kmh, track.heading), abs=2.0
        )
        assert newest["heading"] == pytest.approx(track.heading, abs=1.0)
    # Pushes follow the engine: A moved east between pushes while it was live.
    a_lons = [
        f["longitude"]
        for msg in flights
        if (f := _live_flight(msg, A.icao)) and f.get("longitude")
    ]
    assert len(set(a_lons)) >= 3
    assert a_lons == sorted(a_lons)


def test_webhook_activate_and_deactivate_for_the_right_aircraft(live):
    posts = live.sink.timed_bodies()
    hooks = [(t, body) for t, body in posts if isinstance(body, dict)]
    assert [b["reason"]["hook"] for _t, b in hooks] == ["activate", "deactivate"], hooks
    (t_on, on), (t_off, off) = hooks
    flight_a = live.flight_id(A.icao)
    for body in (on, off):
        assert body["icao"].lower() == A.icao
        assert body["callsign"] == A.callsign
        assert body["zone"] == "pad"
        assert body["type"] == "low_pass"
        assert body["flight_id"] == flight_a
        assert body["alert_id"].startswith(f"{flight_a}:pad:low_pass:")
    assert on["alert_id"] == off["alert_id"]
    # Activation needs dwell_seconds (1 s) of matching; deactivation came from
    # the climb out of the rule, before the engine was shut down.
    assert t_on - live.play_started >= 1.0
    assert t_off - live.play_started >= CLIMB_AT
    assert t_off < live.shutdown_started
    assert on["telemetry"]["altitude"] == pytest.approx(A.low_alt, abs=8)
    assert off["telemetry"]["altitude"] == pytest.approx(A.high_alt, abs=8)


def test_archive_has_both_flights_with_decoded_telemetry(live):
    for track in (A, B):
        flight_id = live.flight_id(track.icao)
        (flight,) = query(
            live.db, "SELECT * FROM flights WHERE flight_id = ?", (flight_id,)
        )
        assert flight["callsign"] == track.callsign
        assert flight["start_time"] < flight["end_time"]
        rows = query(
            live.db,
            "SELECT * FROM telemetry WHERE flight_id = ? ORDER BY timestamp",
            (flight_id,),
        )
        sent_positions = {
            s.hex
            for s in live.feeder.sent
            if s.hex in live.truth and live.truth[s.hex].icao == track.icao
        }
        # Every received position becomes a sample, except the first frame of
        # each track (a global CPR decode needs its even/odd partner).
        assert len(rows) >= len(sent_positions) - 1, (len(rows), len(sent_positions))
        for row in rows:
            # Join each sample to the raw frame it was decoded from.
            frames = query(
                live.db,
                "SELECT hex FROM frames WHERE icao = ? AND timestamp = ?",
                (track.icao, row["timestamp"]),
            )
            assert frames, f"no raw frame at sample time {row['timestamp']}"
            truth = live.truth[frames[0]["hex"]]
            assert row["icao"] == track.icao
            assert row["latitude"] == pytest.approx(truth.lat, abs=1e-4)
            assert row["longitude"] == pytest.approx(truth.lon, abs=1e-4)
            assert row["altitude"] == pytest.approx(truth.alt_m, abs=0.5)
    # Metadata from the (faked) HexDB lookup reached the archive.
    (a_row,) = query(live.db, "SELECT * FROM flights WHERE icao = ?", (A.icao,))
    assert (
        a_row["model"],
        a_row["owner"],
        a_row["country"],
        a_row["aircraft_type"],
    ) == (
        "A320 214",
        "E2E Air",
        "Switzerland",
        "A320",
    )
    assert not live.external.blocked, live.external.blocked


def test_archive_stores_the_registration(live):
    (a_row,) = query(
        live.db, "SELECT registration FROM flights WHERE icao = ?", (A.icao,)
    )
    assert a_row["registration"] == "HB-JZA"


def test_archive_labels_samples_with_receivers(live):
    a_rows = query(live.db, "SELECT receivers FROM telemetry WHERE icao = ?", (A.icao,))
    assert a_rows and {r["receivers"] for r in a_rows} == {'["roof"]'}
    b_labels = [
        json.loads(r["receivers"])
        for r in query(
            live.db, "SELECT receivers FROM telemetry WHERE icao = ?", (B.icao,)
        )
    ]
    assert b_labels
    assert all(set(labels) <= {"roof", "mast"} and labels for labels in b_labels)
    # Heard by both: the stronger copy (roof) is listed first.
    assert ["roof", "mast"] in b_labels
    # While roof was disconnected, B was still tracked through mast alone.
    assert ["mast"] in b_labels
    # mast's antenna position came from its receiver.json.
    mast = live.engine.positions.get("mast")
    assert (mast.source, mast.latitude, mast.longitude) == (
        "receiver.json",
        MAST[0],
        MAST[1],
    )


def test_archive_integrity_status_per_sample_matches_verdict_log(live):
    allowed = {None, "unknown", "ok", "suspect", "spoofed"}
    for track in (A, B):
        flight_id = live.flight_id(track.icao)
        verdicts = query(
            live.db,
            "SELECT * FROM integrity WHERE flight_id = ? ORDER BY timestamp",
            (flight_id,),
        )
        assert verdicts, f"no integrity verdicts logged for {track.icao}"
        samples = query(
            live.db,
            "SELECT timestamp, integrity_status, integrity_score, integrity_flags "
            "FROM telemetry WHERE flight_id = ? ORDER BY timestamp",
            (flight_id,),
        )
        assert {s["integrity_status"] for s in samples} <= allowed
        for sample in samples:
            effective = [v for v in verdicts if v["timestamp"] <= sample["timestamp"]]
            if not effective:
                assert sample["integrity_status"] is None
                continue
            verdict = effective[-1]
            assert sample["integrity_status"] == verdict["status"]
            assert sample["integrity_score"] == pytest.approx(verdict["score"])
            assert json.loads(sample["integrity_flags"]) == json.loads(verdict["flags"])
        # Samples after the first verdict carry one.
        assert samples[-1]["integrity_status"] is not None
        (flight,) = query(
            live.db, "SELECT info FROM flights WHERE flight_id = ?", (flight_id,)
        )
        assert (
            json.loads(flight["info"])["integrity"]["status"] == verdicts[-1]["status"]
        )


def test_archive_has_the_alert_episode(live):
    flight_a = live.flight_id(A.icao)
    rows = query(live.db, "SELECT * FROM alerts")
    assert len(rows) == 1, [dict(r) for r in rows]
    (alert,) = rows
    webhook_id = live.sink.timed_bodies()[0][1]["alert_id"]
    assert alert["alert_id"] == webhook_id
    assert alert["flight_id"] == flight_a
    assert alert["zone"] == "pad"
    assert alert["rule"] == "low_pass"
    assert not alert["active"]
    assert alert["activated_at"] < alert["deactivated_at"]


def test_raw_archive_has_every_received_frame_copy(live):
    archived = query(live.db, "SELECT receiver, hex, df, icao, rssi, clock FROM frames")
    got = sorted((r["receiver"], r["hex"], r["clock"]) for r in archived)
    want = sorted((s.receiver, s.hex, s.clock) for s in live.feeder.sent)
    assert got == want
    for row in archived:
        assert row["df"] == 17
        assert row["icao"] == row["hex"][2:8]
    # Escaped clock and signal bytes survived the Beast parser.
    a_rows = [r for r in archived if r["icao"] == A.icao]
    assert all(r["clock"] >> 32 == 0x1A1A for r in a_rows)
    assert all(r["rssi"] == pytest.approx(-19.83, abs=0.01) for r in a_rows)


def test_shutdown_was_prompt_and_left_no_live_state(live):
    assert live.shutdown_seconds < 10
    assert live.engine.tracker.planes == {}
    assert not live.redis.sets.get("live:flights")
    assert "live:engine" not in live.redis.kv


# --- 2. portal read path -----------------------------------------------------------


def test_ready_is_healthy_while_the_engine_runs(live):
    body = live.ready_running
    assert body["_status_code"] == 200
    assert body["status"] == "ok"
    assert body["redis"] is True and body["history"] is True and body["engine"] is True
    stopped = live.ready_stopped
    assert stopped["_status_code"] == 200
    assert stopped["engine"] is False and stopped["status"] == "degraded"


def test_ws_live_hello_snapshot_and_pushes(live):
    messages = [m for _t, m in live.live_messages]
    hello = messages[0]
    assert hello["type"] == "hello"
    assert hello["protocol"] == "pyaerial.live"
    assert {"flights", "alerts", "telemetry", "stats"} <= set(hello["streams"])
    # Snapshot right after hello, before any aircraft existed.
    assert [m["type"] for m in messages[1:4]] == ["flights", "alerts", "stats"]
    assert messages[1]["flights"] == []
    types = [m["type"] for m in messages]
    assert types.count("flights") >= 3
    # The engine's alert episode was pushed live, with the webhook's id.
    alert_id = live.sink.timed_bodies()[0][1]["alert_id"]
    pushed = [a for m in messages if m["type"] == "alerts" for a in m["alerts"]]
    assert any(a["alert_id"] == alert_id and a["active"] for a in pushed)
    assert any(a["alert_id"] == alert_id and not a["active"] for a in pushed)
    flights_with_alert = [
        f
        for m in messages
        if m["type"] == "flights"
        for f in m["flights"]
        if any(x["alert_id"] == alert_id for x in f.get("active_alerts", []))
    ]
    assert flights_with_alert and {f["icao"].lower() for f in flights_with_alert} == {
        A.icao
    }
    # Incremental telemetry pushes for both aircraft, matching the archive.
    points = [p for m in messages if m["type"] == "telemetry" for p in m["telemetry"]]
    assert {p["icao"].lower() for p in points} == {A.icao, B.icao}
    archived = {r["timestamp"]: r for r in query(live.db, "SELECT * FROM telemetry")}
    for point in points:
        row = archived[point["timestamp"]]
        assert (point["latitude"], point["longitude"]) == (
            row["latitude"],
            row["longitude"],
        )
    # Once the engine stopped, the live flight list emptied.
    last_flights = [m for m in messages if m["type"] == "flights"][-1]
    assert last_flights["flights"] == []
    stats = [m["stats"] for m in messages if m["type"] == "stats"]
    assert any(s["engine_live"] and s["live_flights"] == 2 for s in stats)


def test_history_rpc_returns_archived_flights_and_telemetry(live):
    from e2e_support import ws_rpc

    flight_a, flight_b = live.flight_id(A.icao), live.flight_id(B.icao)
    reply = ws_rpc(live.portal.ws, "fetchFlights", {"view": "history", "limit": 50})
    assert reply["success"], reply
    by_id = {f["flight_id"]: f for f in reply["data"]}
    assert {flight_a, flight_b} <= set(by_id)
    assert by_id[flight_a]["callsign"] == A.callsign
    assert by_id[flight_a]["registration"] == "HB-JZA"
    assert by_id[flight_b]["callsign"] == B.callsign

    detail = ws_rpc(
        live.portal.ws, "fetchFlight", {"flightId": flight_b, "view": "history"}
    )
    assert detail["success"] and detail["data"]["flight_id"] == flight_b

    archived = count(
        live.db, "SELECT COUNT(*) FROM telemetry WHERE flight_id = ?", (flight_b,)
    )
    full = ws_rpc(
        live.portal.ws,
        "fetchTelemetry",
        {"flightId": flight_b, "view": "history", "limit": 20000},
    )
    assert full["success"] and full["truncated"] is False
    assert len(full["data"]) == archived
    timestamps = [p["timestamp"] for p in full["data"]]
    assert timestamps == sorted(timestamps)
    assert all(p["flight_id"] == flight_b and p.get("receivers") for p in full["data"])
    # The verdict in effect is included once there is one (none before).
    rows = {
        r["timestamp"]: r
        for r in query(
            live.db, "SELECT * FROM telemetry WHERE flight_id = ?", (flight_b,)
        )
    }
    for point in full["data"]:
        assert (
            point.get("integrity_status")
            == rows[point["timestamp"]]["integrity_status"]
        )
    assert full["data"][-1]["integrity_status"] is not None

    page = ws_rpc(
        live.portal.ws,
        "fetchTelemetry",
        {"flightId": flight_b, "view": "history", "limit": 3},
    )
    assert page["success"] and page["truncated"] is True
    # Without a cursor the newest points are kept (the path ends at the last fix).
    assert [p["timestamp"] for p in page["data"]] == timestamps[-3:]

    alerts = ws_rpc(live.portal.ws, "fetchAlerts", {"view": "history"})
    assert alerts["success"]
    assert [a["flight_id"] for a in alerts["data"]] == [flight_a]

    # The portal has no REST history routes; history is served over /ws/live.
    spec = httpx.get(f"{live.portal.http}/api", timeout=5).json()
    assert {"fetchFlights", "fetchTelemetry", "fetchAlerts"} <= set(spec["actions"])


@pytest.mark.parametrize("feed", ["tcp", "websocket"])
def test_beast_reserve_feed_is_correctly_escaped(live, feed):
    data = live.beast_tcp_bytes if feed == "tcp" else live.beast_ws_bytes
    assert data, f"no Beast bytes on {feed}"
    strict = strict_beast_decode(data)
    parsed = BeastParser().feed(data)
    assert [f.hex for f in strict] == [h for h, _rssi, _clock in parsed]
    sent_hexes = {s.hex for s in live.feeder.sent}
    assert {f.hex for f in strict} == sent_hexes
    assert all(es_crc_ok(f.hex) for f in strict)
    # A's frames went out with the 0x1a signal byte, which must be doubled.
    a_frames = [f for f in strict if f.hex[2:8] == A.icao]
    assert a_frames and all(f.level == SIGNAL_ESC for f in a_frames)
    assert all(f.escapes >= 3 for f in a_frames)  # 2 ICAO bytes + signal
    # Copies heard by both receivers are merged: each B position goes out once,
    # with the stronger copy's signal (roof), or mast's while roof was down.
    heard: dict[str, set[str]] = {}
    for sent in live.feeder.sent:
        heard.setdefault(sent.hex, set()).add(sent.receiver)
    b_positions = [f for f in strict if f.hex[2:8] == B.icao and f.hex in live.truth]
    assert b_positions
    assert len({f.hex for f in b_positions}) == len(b_positions)
    for frame in b_positions:
        assert frame.level == (0x40 if "roof" in heard[frame.hex] else 0x20)
    assert {f.level for f in b_positions} == {0x40, 0x20}


def test_live_telemetry_push_carries_provenance(live):
    messages = [m for _t, m in live.live_messages]
    points = [p for m in messages if m["type"] == "telemetry" for p in m["telemetry"]]
    assert points
    archived = {r["timestamp"]: r for r in query(live.db, "SELECT * FROM telemetry")}
    for point in points:
        row = archived[point["timestamp"]]
        assert point.get("receivers") == json.loads(row["receivers"])
        assert point.get("integrity_status") == row["integrity_status"]


def test_live_fetch_telemetry_carries_provenance(live):
    reply = live.live_telemetry_rpc
    assert reply["success"] and reply["truncated"] is False, reply
    assert reply["data"]
    assert all(p["flight_id"].startswith(B.icao) for p in reply["data"])
    assert all(set(p["receivers"]) <= {"roof", "mast"} for p in reply["data"])


# --- restart ------------------------------------------------------------------------


def test_new_engine_restarts_on_the_same_archive(live):
    before = {
        icao: count(live.db, "SELECT COUNT(*) FROM telemetry WHERE icao = ?", (icao,))
        for icao in (A.icao, B.icao)
    }
    config = load_config(live.config_path)
    engine = Engine(config, aircraft_db_path=str(live.tmp / "aircraft.db"))
    assert engine.history_store.ping()
    accepts = dict(live.feeder.accepts)
    runner = EngineThread(engine).start()
    try:
        wait_for(
            lambda: all(live.feeder.accepts[n] > accepts[n] for n in ("roof", "mast")),
            10,
            message="second engine's receivers to connect",
        )
        events, _truth = build_script([C], 2.0, seed=7)
        live.feeder.play(events).join(10)
        wait_for(
            lambda: (
                count(live.db, "SELECT COUNT(*) FROM frames WHERE icao = ?", (C.icao,))
                >= len(events)
            ),
            10,
            message="second engine to archive C's frames",
        )
    finally:
        runner.stop()
    flights = {r["icao"]: r for r in query(live.db, "SELECT * FROM flights")}
    assert set(flights) == {A.icao, B.icao, C.icao}
    assert flights[C.icao]["callsign"] == C.callsign
    assert (
        count(live.db, "SELECT COUNT(*) FROM telemetry WHERE icao = ?", (C.icao,)) >= 2
    )
    after = {
        icao: count(live.db, "SELECT COUNT(*) FROM telemetry WHERE icao = ?", (icao,))
        for icao in (A.icao, B.icao)
    }
    assert after == before
    assert len(query(live.db, "SELECT * FROM alerts")) == 1
