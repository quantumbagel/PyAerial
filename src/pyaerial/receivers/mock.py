"""
Mock receiver plugin: emits simulated ADS-B Mode S messages for testing.
"""
from __future__ import annotations

import math
import time

from pyaerial.receivers import Receiver, register_receiver


def _cpr_nl(lat: float) -> int:
    if abs(lat) >= 87.0:
        return 1
    a = 1 - math.cos(math.pi / 30.0)
    b = math.cos(math.radians(lat)) ** 2
    cos_val = 1 - a / b
    if cos_val < -1:
        return 1
    num = 2 * math.pi / math.acos(cos_val)
    return max(1, int(math.floor(num)))


def encode_airborne_pos(icao: str, lat: float, lon: float, alt_meters: float, is_odd: bool = False) -> str:
    tc = 11
    ss = 0
    nic_sb = 0

    alt_ft = int(round(alt_meters * 3.28084))
    n = int(round((alt_ft + 1000) / 25.0))
    n_high = (n >> 4) & 0x7F
    n_low = n & 0x0F
    alt_code = (n_high << 5) | (1 << 4) | n_low

    dlat = 360.0 / (59.0 if is_odd else 60.0)
    yz = int(round((2**17) * ((lat % dlat) / dlat))) & 0x1FFFF

    nl = _cpr_nl(lat)
    dlon = 360.0 / max(1.0, (nl - 1) if is_odd else nl)
    xz = int(round((2**17) * ((lon % dlon) / dlon))) & 0x1FFFF

    time_bit = 0
    f_bit = 1 if is_odd else 0

    me = (tc << 51) | (ss << 49) | (nic_sb << 48) | (alt_code << 36) | (time_bit << 35) | (f_bit << 34) | (yz << 17) | xz
    return f"8D{icao.upper()}{me:014X}000000"


def _char_to_code(c: str) -> int:
    if "A" <= c <= "Z":
        return ord(c) - ord("A") + 1
    if "0" <= c <= "9":
        return ord(c) - ord("0") + 48
    return 32


def encode_callsign(icao: str, callsign: str) -> str:
    cs = callsign.ljust(8)[:8].upper()
    code = 0
    for ch in cs:
        code = (code << 6) | _char_to_code(ch)
    me = (4 << 51) | code
    return f"8D{icao.upper()}{me:014X}000000"


def encode_velocity(icao: str, speed_kmh: float, heading_deg: float, vert_rate_fps: float = 0.0) -> str:
    tc = 19
    st = 1
    ic = 0
    resv = 0
    nac = 1

    speed_kts = speed_kmh * 0.539957
    rad = math.radians(heading_deg)
    v_ew = speed_kts * math.sin(rad)
    v_ns = speed_kts * math.cos(rad)

    dir_ew = 1 if v_ew < 0 else 0
    val_ew = int(round(abs(v_ew))) + 1

    dir_ns = 1 if v_ns < 0 else 0
    val_ns = int(round(abs(v_ns))) + 1

    vr_dir = 1 if vert_rate_fps < 0 else 0
    vr_val = int(round(abs(vert_rate_fps) / 64.0)) + 1

    me = (tc << 51) | (st << 48) | (ic << 47) | (resv << 46) | (nac << 43) | (dir_ew << 42) | ((val_ew & 0x3FF) << 32) | (dir_ns << 31) | ((vr_dir) << 10) | ((vr_val & 0x1FF) << 1)
    return f"8D{icao.upper()}{me:014X}000000"


@register_receiver("mock")
class MockReceiver(Receiver):
    """Emits simulated ADS-B Mode S messages for testing."""

    def configure(self, arguments: dict) -> None:
        self.interval = float(arguments.get("interval", 0.5))

    def run(self) -> str | None:
        self.log.info("Starting mock ADS-B data feeder...")

        planes = [
            {
                "icao": "A1B2C3",
                "callsign": "N123AB",
                "center_lat": 35.7275,
                "center_lon": -78.6959,
                "radius": 0.005,
                "altitude": 1200.0,
                "speed": 180.0,
                "speed_rad": 0.04,
                "phase": 0.0,
            },
            {
                "icao": "B4C5D6",
                "callsign": "DRONE01",
                "center_lat": 35.7285,
                "center_lon": -78.6965,
                "radius": 0.003,
                "altitude": 120.0,
                "speed": 45.0,
                "speed_rad": 0.08,
                "phase": 2.0,
            },
            {
                "icao": "C7D8E9",
                "callsign": "MEDEVAC1",
                "center_lat": 35.8000,
                "center_lon": -78.7500,
                "radius": 0.080,
                "altitude": 450.0,
                "speed": 210.0,
                "speed_rad": 0.02,
                "phase": 4.0,
            },
            {
                "icao": "D1E2F3",
                "callsign": "COOLFLY1",
                "center_lat": 35.8500,
                "center_lon": -78.8000,
                "radius": 0.120,
                "altitude": 2200.0,
                "speed": 260.0,
                "speed_rad": 0.015,
                "phase": 1.0,
            },
            {
                "icao": "D9E0F1",
                "callsign": "PIPER88",
                "center_lat": 35.7500,
                "center_lon": -78.7000,
                "radius": 0.015,
                "altitude": 4500.0,
                "speed": 165.0,
                "speed_rad": 0.03,
                "phase": 5.0,
            },
        ]

        for plane in planes:
            c_msg = encode_callsign(plane["icao"], plane["callsign"])
            self.emit(c_msg, time.time())

        tick_count = 0
        while not self.should_stop():
            now = time.time()
            tick_count += 1

            for plane in planes:
                plane["phase"] += plane["speed_rad"]
                p = plane["phase"]
                lat = plane["center_lat"] + math.sin(p) * plane["radius"]
                lon = plane["center_lon"] + math.cos(p) * plane["radius"]
                heading = (math.degrees(math.atan2(math.cos(p), -math.sin(p))) + 360) % 360

                if tick_count % 10 == 1:
                    c_msg = encode_callsign(plane["icao"], plane["callsign"])
                    self.emit(c_msg, now)

                p_msg = encode_airborne_pos(plane["icao"], lat, lon, plane["altitude"], is_odd=(tick_count % 2 == 1))
                self.emit(p_msg, now)

                v_msg = encode_velocity(plane["icao"], plane["speed"], heading)
                self.emit(v_msg, now)

            time.sleep(self.interval)

        return None
