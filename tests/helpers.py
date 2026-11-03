"""Shared test factories: configs, Mode S frames, live planes, engines."""

from __future__ import annotations

import json
import math
from typing import Any

from pyModeS.position import cprNL
from pyModeS.util import crc

from pyaerial.config.schema import (
    ArchiveConfig,
    Config,
    FieldConstraint,
    HomeConfig,
    ReceiverConfig,
    ReceiverPositionConfig,
    RuleConfig,
    TrackingConfig,
    WebConfig,
    ZoneConfig,
)
from pyaerial.constants import (
    STORE_ALT,
    STORE_GNSS_HEIGHT,
    STORE_HEADING,
    STORE_ICAO,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.engine import Engine
from pyaerial.models import AircraftInfo, Datum, Plane, Series, TrackState, Verdict
from pyaerial.receivers.positions import ReceiverPositions
from pyaerial.tracker import Tracker


def make_rule(
    name: str = "warn",
    *,
    retain: bool = True,
    dwell_seconds: int = 60,
    hysteresis_seconds: float = 0,
    **when_fields: dict,
) -> RuleConfig:
    when = {
        key: FieldConstraint.model_validate(spec) for key, spec in when_fields.items()
    }
    if not when:
        when = {
            "altitude": FieldConstraint(maximum=2000),
            "eta": FieldConstraint(maximum=120),
        }
    return RuleConfig(
        name=name,
        when=when,
        dwell_seconds=dwell_seconds,
        retain=retain,
        hysteresis_seconds=hysteresis_seconds,
    )


def make_config(
    *,
    zones: dict | None = None,
    tracking: TrackingConfig | None = None,
    receivers: dict | None = None,
    archive: ArchiveConfig | None = None,
) -> Config:
    if zones is None:
        zones = {
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[make_rule(altitude={"max": 2000}, eta={"max": 120})],
            )
        }
    return Config(
        home=HomeConfig(latitude=35.7275, longitude=-78.6959),
        receivers=receivers or {"main": ReceiverConfig(type="dump1090")},
        zones=zones,
        tracking=tracking or TrackingConfig(),
        archive=archive or ArchiveConfig(),
        web=WebConfig(beast_port=None),
    )


# --- live planes -------------------------------------------------------------


def make_plane(
    icao: str = "abc123",
    first: float = 1.0,
    last: float | None = None,
    *,
    recv: Series | None = None,
    calc: Series | None = None,
    **info: Any,
) -> Plane:
    """A live plane first heard at *first* (last at *last*, default *first*).

    *info* sets :class:`AircraftInfo` fields (callsign, registration, ...).
    """
    return Plane(
        info=AircraftInfo(icao=icao, **info),
        track=TrackState(
            first_packet=first, last_update=first if last is None else last
        ),
        received_data=recv if recv is not None else {},
        calculated_data=calc if calc is not None else {},
    )


def track_plane(
    icao: str = "abc123",
    first: float = 1.0,
    samples: int = 3,
    *,
    step: float = 1.0,
    lat_step: float = 0.01,
) -> Plane:
    """A live plane with *samples* positions *step* seconds apart from *first*.

    Latitude climbs *lat_step* degrees per sample from 35.0; longitude stays
    at -78.0.
    """
    times = [first + n * step for n in range(samples)]
    return make_plane(
        icao,
        first,
        times[-1],
        recv={
            STORE_LAT: [Datum(35.0 + n * lat_step, t) for n, t in enumerate(times)],
            STORE_LONG: [Datum(-78.0, t) for t in times],
        },
    )


def fine_track_plane(
    icao: str = "abc123", first: float = 1.0, samples: int = 3, step: float = 1.0
) -> Plane:
    """``track_plane`` with positions 1e-4 degrees (~11 m) apart."""
    return track_plane(icao, first, samples, step=step, lat_step=1e-4)


def one_fix_plane(icao: str = "abc123", first: float = 1.0) -> Plane:
    """A live plane with a single position at *first*."""
    return make_plane(
        icao,
        first,
        recv={
            STORE_LAT: [Datum(35.0, first)],
            STORE_LONG: [Datum(-78.0, first)],
        },
    )


def heading_plane(icao: str = "abc123", first: float = 1.0, samples: int = 1) -> Plane:
    """Stationary positions and a calculated heading, one per second."""
    return make_plane(
        icao,
        first,
        first + samples,
        recv={
            STORE_LAT: [Datum(35.0, first + n) for n in range(samples)],
            STORE_LONG: [Datum(-78.0, first + n) for n in range(samples)],
        },
        calc={STORE_HEADING: [Datum(90.0, first + n) for n in range(samples)]},
    )


def pad_plane(first: float, samples: int = 721, step: float = 10.0) -> Plane:
    """A plane parked inside the ``pad`` zone: one position every *step* s."""
    times = [first + n * step for n in range(samples)]
    return make_plane(
        "abc123",
        first,
        times[-1],
        recv={
            STORE_LAT: [Datum(35.725, t) for t in times],
            STORE_LONG: [Datum(-78.695, t) for t in times],
        },
    )


def gnss_then_baro_plane(first: float = 1.0) -> Plane:
    """GNSS (TC 20-22) positions for 30 s, then baro (TC 9-18) positions."""
    plane = track_plane("abc123", first, samples=0 + 1)
    recv = plane.received_data
    recv[STORE_LAT] = []
    recv[STORE_LONG] = []
    for n in range(61):
        t = first + n
        recv[STORE_LAT].append(Datum(35.0 + n * 1e-3, t))
        recv[STORE_LONG].append(Datum(-78.0 + n * 1e-3, t))
    # Level at 10972.8 m for t=first..first+30: the tracker keeps a constant
    # run as its first and last datum only.
    recv[STORE_GNSS_HEIGHT] = [Datum(10972.8, first), Datum(10972.8, first + 30)]
    recv[STORE_ALT] = [Datum(10900.0, first + 31), Datum(10890.0, first + 60)]
    plane.track.last_update = first + 60
    return plane


def with_unknown_then_ok(plane: Plane) -> Plane:
    """Give *plane* an ``unknown`` (score-less) verdict, then an ``ok`` one."""
    first = plane.track.first_packet
    plane.track.integrity_log = [
        Verdict(first - 0.5, None, "unknown", (), {}),
        Verdict(first + 1.5, 0.9, "ok", (), {"kinematics": {"status": "pass"}}),
    ]
    plane.track.position_receivers = {first + n: ("roof", "mast") for n in range(3)}
    return plane


# --- alerts and engines --------------------------------------------------------


def pad_config(tmp_path) -> Config:
    """One retained, 1 s dwell ``warn`` rule on ``pad``; archive in *tmp_path*."""
    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[make_rule(name="warn", retain=True, dwell_seconds=1)],
            )
        }
    )
    config.database.path = str(tmp_path / "pyaerial.db")
    return config


def alert_meta(icao: str = "abc123") -> dict:
    return {STORE_ICAO: icao, "zone": "pad", "type": "warn"}


def alert_doc(alert_id: str, flight_id: str, *, active: bool, at: float) -> str:
    """A stored alert episode document, as JSON."""
    return json.dumps(
        {
            "alert_id": alert_id,
            "flight_id": flight_id,
            "icao": flight_id.split("-")[0],
            "zone": "pad",
            "rule": "warn",
            "active": active,
            "activated_at": at,
            "deactivated_at": None if active else at + 10,
            "last_updated": at + 10,
            "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
        }
    )


def make_engine(tmp_path, **config_kwargs) -> Engine:
    """An isolated (memory live store) engine on ``make_config(**config_kwargs)``."""
    return Engine(
        make_config(**config_kwargs),
        isolated=True,
        aircraft_db_path=str(tmp_path / "aircraft.db"),
    )


class InlineDispatcher:
    """Stands in for an AlertEngine's dispatcher: runs each job on submit."""

    def submit(self, key, fn, *args) -> bool:
        fn(*args)
        return True

    def close(self, timeout: float = 0.0) -> None:
        pass


# Any URI does with the ``fake`` fixture (conftest.py) patched in.
LOCAL_REDIS_URI = "redis://localhost:6379/0"


def tracker_at_home() -> Tracker:
    """A tracker whose only receiver sits at home (35.7275, -78.6959)."""
    config = make_config(
        receivers={
            "nc": ReceiverConfig(
                type="dump1090",
                position=ReceiverPositionConfig(latitude=35.7275, longitude=-78.6959),
            )
        }
    )
    return Tracker(config, positions=ReceiverPositions(config, fetch=False))


def latest_position(tracker: Tracker, icao: str) -> tuple[float, float]:
    recv = tracker.planes[icao].received_data
    return recv[STORE_LAT][-1].value, recv[STORE_LONG][-1].value


# --- Mode S frames -----------------------------------------------------------

IDENT = "8d4840d6202cc371c32ce0576098"  # KLM1023, valid CRC
DF17_HEX = "8d406b902015a678d4d220aa4bda"  # a valid DF17 squitter


def with_crc(hex_msg: str) -> str:
    """*hex_msg* (112-bit) with its parity recomputed, e.g. after editing DF/CF."""
    body = f"{hex_msg[:22]}000000"
    return f"{int(body, 16) | crc(body):028x}"


def airborne_position(
    icao: str,
    lat: float,
    lon: float,
    odd: int,
    *,
    alt_ft: int = 35000,
    first_byte: int = 0x8D,
    imf: int = 0,
    tc: int = 11,
) -> str:
    """DF17/18 airborne position with a correct CPR encoding and CRC.

    The altitude is AC12-coded (Q=1, 25 ft) for every typecode.
    """
    nb = 1 << 17
    dlat = 360.0 / (60 - odd)
    yz = math.floor(nb * (lat % dlat) / dlat + 0.5)
    rlat = dlat * (yz / nb + math.floor(lat / dlat))
    ni = cprNL(rlat) - odd
    dlon = 360.0 / ni if ni > 0 else 360.0
    xz = math.floor(nb * (lon % dlon) / dlon + 0.5)
    n = (alt_ft + 1000) // 25
    alt12 = ((n >> 4) << 5) | (1 << 4) | (n & 0xF)
    me = (tc << 51) | (imf << 48) | (alt12 << 36) | (odd << 34)
    me |= ((yz % nb) << 17) | (xz % nb)
    return with_crc(f"{first_byte:02x}{icao}{me:014x}000000")
