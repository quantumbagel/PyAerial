from __future__ import annotations

import sqlite3
from pathlib import Path
import logging
from pyaerial.constants import (
    STORE_INFO,
    STORE_INTERNAL,
    STORE_FIRST_PACKET,
    STORE_MOST_RECENT_PACKET,
    STORE_ICAO,
    STORE_CALLSIGN,
    STORE_LAT,
    STORE_LONG,
    STORE_ALT,
    STORE_HORIZ_SPEED,
    STORE_HEADING,
    STORE_RECV_DATA,
    STORE_CALC_DATA,
)
from pyaerial.models import get_latest

log = logging.getLogger("pyaerial.ipc_db")

class IpcDB:
    def __init__(self, path: str | Path = "live_telemetry.db"):
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self):
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS live_flights (
                    flight_id TEXT PRIMARY KEY,
                    icao TEXT,
                    callsign TEXT,
                    model TEXT,
                    owner TEXT,
                    country TEXT,
                    zone TEXT,
                    level TEXT,
                    start_time REAL,
                    end_time REAL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS live_telemetry (
                    flight_id TEXT,
                    icao TEXT,
                    timestamp REAL,
                    latitude REAL,
                    longitude REAL,
                    altitude REAL,
                    speed REAL,
                    heading REAL,
                    PRIMARY KEY (flight_id, timestamp)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS live_raw_messages (
                    flight_id TEXT,
                    hex TEXT,
                    timestamp REAL,
                    PRIMARY KEY (flight_id, hex, timestamp)
                )
            """)

    def clear_all(self):
        with self._conn:
            self._conn.execute("DELETE FROM live_flights")
            self._conn.execute("DELETE FROM live_telemetry")
            self._conn.execute("DELETE FROM live_raw_messages")
            self._conn.execute("VACUUM")

    def write_active_planes(self, planes: dict[str, dict]):
        with self._conn:
            for icao, plane in planes.items():
                info = plane.get(STORE_INFO, {})
                internal = plane.get(STORE_INTERNAL, {})
                first_packet = internal.get(STORE_FIRST_PACKET, 0.0)
                last_packet = internal.get(STORE_MOST_RECENT_PACKET, 0.0)
                
                # Unique flight identifier for live flight
                flight_id = f"live-{icao.lower()}-{int(first_packet)}"
                
                callsign = info.get(STORE_CALLSIGN) or ""
                model = info.get("model") or ""
                owner = info.get("owner") or ""
                country = info.get("country") or ""
                zone = plane.get("zone") or ""
                level = plane.get("level") or ""

                # Write live flight metadata
                self._conn.execute("""
                    INSERT OR REPLACE INTO live_flights 
                    (flight_id, icao, callsign, model, owner, country, zone, level, start_time, end_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (flight_id, icao.lower(), callsign, model, owner, country, zone, level, first_packet, last_packet))

                # Extract latest telemetry point
                lat_datum = get_latest(STORE_RECV_DATA, STORE_LAT, plane)
                lon_datum = get_latest(STORE_RECV_DATA, STORE_LONG, plane)
                alt_datum = get_latest(STORE_RECV_DATA, STORE_ALT, plane)
                speed_datum = get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane) or get_latest(STORE_RECV_DATA, STORE_HORIZ_SPEED, plane)
                heading_datum = get_latest(STORE_CALC_DATA, STORE_HEADING, plane) or get_latest(STORE_RECV_DATA, STORE_HEADING, plane)

                if lat_datum and lon_datum:
                    lat = lat_datum.value
                    lon = lon_datum.value
                    alt = alt_datum.value if alt_datum else None
                    speed = speed_datum.value if speed_datum else None
                    heading = heading_datum.value if heading_datum else None
                    timestamp = lat_datum.time

                    self._conn.execute("""
                        INSERT OR IGNORE INTO live_telemetry
                        (flight_id, icao, timestamp, latitude, longitude, altitude, speed, heading)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (flight_id, icao.lower(), timestamp, lat, lon, alt, speed, heading))

                # Write raw messages
                raw_msgs = plane.get("raw_messages", [])
                for msg in raw_msgs:
                    self._conn.execute("""
                        INSERT OR IGNORE INTO live_raw_messages
                        (flight_id, hex, timestamp)
                        VALUES (?, ?, ?)
                    """, (flight_id, msg["hex"], msg["timestamp"]))

    def remove_expired_planes(self, icaos: list[str]):
        with self._conn:
            for icao in icaos:
                icao_lower = icao.lower()
                self._conn.execute("DELETE FROM live_flights WHERE icao = ?", (icao_lower,))
                self._conn.execute("DELETE FROM live_telemetry WHERE icao = ?", (icao_lower,))
                self._conn.execute("DELETE FROM live_raw_messages WHERE icao = ?", (icao_lower,))

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
