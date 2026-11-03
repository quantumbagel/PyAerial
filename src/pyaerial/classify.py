"""
ADS-B / Mode S message classification.

Turns a raw hex message into a :class:`ClassifiedMessage`: the aircraft's
identity fields, received telemetry values, and a typecode category used for
internal bookkeeping.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeGuard

import pyModeS as pms
from pyModeS.util import typecode as pms_typecode, icao as pms_icao

from pyaerial.config.schema import HomeConfig
from pyaerial.constants import (
    MAX_CPR_JUMP_DEG,
    MAX_SPEED_MPS,
    STORE_ALT,
    STORE_GNSS_HEIGHT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
    STORE_VERT_SPEED,
)
from pyaerial.units import FT_PER_MIN_TO_MPS, FT_TO_M, KT_TO_KMH

log = logging.getLogger("pyaerial.classify")

# Internal packet-type buckets used for status reporting.
CAT_IDENT = 1
CAT_SURFACE = 2
CAT_AIRBORNE_BARO = 3
CAT_AIRBORNE_GNSS = 4
CAT_VELOCITY = 5

_M_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True, slots=True)
class ClassifiedMessage:
    """One decoded message, ready to merge into a :class:`~pyaerial.models.Plane`."""

    icao: str
    typecode_category: int
    # received_data field (STORE_LAT, STORE_ALT, ...) -> decoded value; fields
    # the message carries but could not decode are left out.
    values: dict[str, Any] = field(default_factory=dict)
    # Identification messages only: [typecode, ADS-B emitter category], and
    # the callsign when it is not blank.
    plane_category: list[Any] | None = None
    callsign: str | None = None


def max_jump_deg(elapsed_s: float | None) -> float:
    """Largest believable move (degrees of latitude) since the last fix.

    Never below :data:`MAX_CPR_JUMP_DEG`; grows with the time since the last
    fix at :data:`MAX_SPEED_MPS`, so a plane that drops out of coverage and
    reappears further on is not rejected forever.
    """
    if elapsed_s is None or not math.isfinite(elapsed_s) or elapsed_s <= 0:
        return MAX_CPR_JUMP_DEG
    return max(MAX_CPR_JUMP_DEG, elapsed_s * MAX_SPEED_MPS / _M_PER_DEG_LAT)


def airborne_cpr(msg: str) -> tuple[int, int, int] | None:
    """``(odd, lat, lon)`` raw CPR fields of a DF17/18 airborne position frame.

    ``None`` for any other typecode. The caller has checked DF and CRC.
    """
    if len(msg) != 28:
        return None
    try:
        me = int(msg[8:22], 16)
    except ValueError:
        return None
    typecode = me >> 51
    if not (9 <= typecode <= 18 or 20 <= typecode <= 22):
        return None
    return (me >> 34) & 1, (me >> 17) & 0x1FFFF, me & 0x1FFFF


def classify(
    msg: str,
    home: HomeConfig,
    last_position: tuple[float, float] | None = None,
    *,
    jump_deg: float = MAX_CPR_JUMP_DEG,
    on_implausible: Callable[[], None] | None = None,
    icao: str | None = None,
    reference: tuple[float, float] | None = None,
    pair_position: tuple[float, float] | None = None,
) -> ClassifiedMessage | None:
    """
    Classify a single ADS-B message.

    Assumes downlink format 17 or 18 with a valid CRC (the tracker checks).
    Returns ``None`` for messages that should be ignored (invalid ICAO,
    unsupported typecode, etc.). A position further than ``jump_deg`` from
    ``last_position`` is dropped and ``on_implausible`` is called so the
    caller can count consecutive rejections.

    *icao* skips re-deriving the address when the caller already has it.
    *pair_position* is this airborne frame's position from a global even/odd
    CPR decode; it is the preferred airborne reference because it is valid at
    any range. Otherwise the last position is the reference, then *reference*
    (the receiving antenna), then ``home``. Local decoding is only correct
    within 180 NM (airborne) / 45 NM (surface) of the reference.
    """
    try:
        typecode = pms_typecode(msg)
    except Exception:
        return None

    # pyModeS v2 used -1; v3 returns None for non-DF17/18 (short Mode S, DF11).
    if typecode is None or typecode == -1:
        return None

    if icao is None:
        try:
            icao = pms_icao(msg)
        except Exception:
            return None
    if not _valid_icao(icao):
        return None

    values: dict[str, Any] | None = None
    category: int | None = None
    plane_category: list[Any] | None = None
    ident_callsign: str | None = None

    if 1 <= typecode <= 4:
        try:
            decoded = pms.decode(msg)
        except Exception:
            return None
        ca = decoded.get("category")
        callsign = decoded.get("callsign") or ""
        callsign = callsign.replace("_", "").strip()
        plane_category = [typecode, ca]
        ident_callsign = callsign or None
        values = {}
        category = CAT_IDENT

    elif 5 <= typecode <= 8:
        ref = last_position or reference or (home.latitude, home.longitude)
        try:
            decoded = pms.decode(msg, surface_ref=ref)
        except Exception:
            return None
        lat = decoded.get("latitude")
        lon = _wrap_longitude(decoded.get("longitude"))
        if not _plausible_fix(lat, lon, last_position, jump_deg):
            if on_implausible is not None:
                on_implausible()
            return None
        speed = decoded.get("groundspeed")
        angle = decoded.get("track")
        values = {
            STORE_LAT: lat,
            STORE_LONG: lon,
            STORE_HORIZ_SPEED: speed * KT_TO_KMH if speed is not None else None,
            STORE_HEADING: angle,
        }
        category = CAT_SURFACE

    elif 9 <= typecode <= 18 or 20 <= typecode <= 22:
        ref = (
            pair_position
            or last_position
            or reference
            or (home.latitude, home.longitude)
        )
        try:
            decoded = pms.decode(msg, reference=ref)
        except Exception:
            return None
        lat = decoded.get("latitude")
        lon = _wrap_longitude(decoded.get("longitude"))
        if not _plausible_fix(lat, lon, last_position, jump_deg):
            if on_implausible is not None:
                on_implausible()
            return None
        recv: dict[str, Any] = {STORE_LAT: lat, STORE_LONG: lon}
        if typecode <= 18:
            alt = decoded.get("altitude")
            recv[STORE_ALT] = alt * FT_TO_M if alt is not None else None
            category = CAT_AIRBORNE_BARO
        else:
            gnss_ft = _gnss_height_ft(msg)
            recv[STORE_GNSS_HEIGHT] = gnss_ft * FT_TO_M if gnss_ft is not None else None
            category = CAT_AIRBORNE_GNSS
        values = recv

    elif typecode == 19:
        try:
            decoded = pms.decode(msg)
        except Exception:
            return None
        speed = decoded.get("groundspeed")
        angle = decoded.get("track")
        vert_rate = decoded.get("vertical_rate")
        values = {
            STORE_HORIZ_SPEED: speed * KT_TO_KMH if speed is not None else None,
            STORE_HEADING: angle,
            STORE_VERT_SPEED: vert_rate * FT_PER_MIN_TO_MPS
            if vert_rate is not None
            else None,
        }
        category = CAT_VELOCITY

    elif typecode in (28, 29, 31):
        return None

    if values is None or category is None:
        log.debug("Unsupported typecode %s (msg=%s)", typecode, msg)
        return None

    classified = ClassifiedMessage(
        icao=icao,
        typecode_category=category,
        values={k: v for k, v in values.items() if v is not None},
        plane_category=plane_category,
        callsign=ident_callsign,
    )
    log.debug("Classified typecode %s: %s", typecode, classified)
    return classified


def _gnss_height_ft(msg: str) -> int | None:
    """GNSS height (HAE, ft) of a TC 20-22 airborne position.

    The 12-bit field uses the same AC coding as barometric altitude (25 ft
    with Q=1), as decoded by dump1090, readsb and rs1090. pyModeS reads it
    as whole metres instead.
    """
    return ac12_altitude_ft((int(msg[8:22], 16) >> 36) & 0xFFF)


def ac12_altitude_ft(ac: int) -> int | None:
    """Decode a 12-bit ADS-B altitude code (ME bits 9-20) to feet.

    Bit layout, MSB first: C1 A1 C2 A2 C4 A4 B1 Q B2 D2 B4 D4 (the 13-bit
    Mode S AC field without its M bit). Q=1 is 25 ft steps from -1000 ft;
    Q=0 is 100 ft Gillham (Gray) code. ``None`` for "no altitude" (all
    zero) and for invalid Gillham codes. Matches pyModeS's decoder for all
    4096 codes.
    """
    if ac == 0:
        return None
    if ac & 0x10:
        return (((ac & 0xFE0) >> 1) | (ac & 0xF)) * 25 - 1000

    def bit(pos: int) -> int:
        return (ac >> (11 - pos)) & 1

    c1, a1, c2, a2, c4, a4, b1 = (bit(pos) for pos in range(7))
    b2, d2, b4, d4 = bit(8), bit(9), bit(10), bit(11)
    gc500 = (
        (d2 << 7) | (d4 << 6) | (a1 << 5) | (a2 << 4)
        | (a4 << 3) | (b1 << 2) | (b2 << 1) | b4
    )
    n500 = _gray_to_int(gc500)
    n100 = _gray_to_int((c1 << 2) | (c2 << 1) | c4)
    if n100 in (0, 5, 6):
        return None
    if n100 == 7:
        n100 = 5
    if n500 % 2:
        n100 = 6 - n100
    return n500 * 500 + n100 * 100 - 1300


def _gray_to_int(n: int) -> int:
    n ^= n >> 8
    n ^= n >> 4
    n ^= n >> 2
    n ^= n >> 1
    return n


def _valid_icao(icao: object) -> TypeGuard[str]:
    return isinstance(icao, str) and len(icao) == 6 and icao != "000000"


def _finite_coord(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _wrap_longitude(value: object) -> object:
    """Fold a finite longitude into [-180, 180).

    Local CPR decoding returns a longitude within half a zone of the
    reference, so near the antimeridian it can exceed +/-180.
    """
    number = _finite_coord(value)
    if number is None:
        return value
    return ((number + 180.0) % 360.0) - 180.0


def _lon_delta_deg(a: float, b: float) -> float:
    delta = abs(a - b) % 360.0
    return min(delta, 360.0 - delta)


def _plausible_fix(
    lat: object,
    lon: object,
    last_position: tuple[float, float] | None,
    jump_deg: float = MAX_CPR_JUMP_DEG,
) -> bool:
    lat_f = _finite_coord(lat)
    lon_f = _finite_coord(lon)
    if lat_f is None or lon_f is None:
        return False
    if last_position is None:
        return True
    # A degree of longitude shrinks with cos(latitude); never gate tighter
    # than jump_deg itself.
    cos_lat = math.cos(math.radians(max(-89.0, min(89.0, last_position[0]))))
    lon_jump = jump_deg / max(cos_lat, 1e-3)
    return (
        abs(lat_f - last_position[0]) <= jump_deg
        and _lon_delta_deg(lon_f, last_position[1]) <= max(jump_deg, lon_jump)
    )

