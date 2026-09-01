"""Live terminal flight table display."""

from __future__ import annotations

import sys
import time
from typing import Any

from pyaerial.config import load_config
from pyaerial.view.store import get_live_store


def format_dump1090_table(live_flights: list[dict[str, Any]]) -> str:
    """Format live flights into a live text table display."""
    now = time.time()
    header = f"{'ICAO':<8} {'Callsign':<10} {'Flight ID':<16} {'Alt (ft)':<10} {'Speed (kt)':<11} {'Track':<7} {'Lat':<10} {'Lon':<11} {'Alerts':<14} {'Status':<8} {'Last Seen':<10}"
    divider = "-" * len(header)
    lines = [header, divider]

    if not live_flights:
        lines.append("No active live flights currently tracked.")
        return "\n".join(lines)

    for flight in live_flights:
        icao = (flight.get("icao") or "N/A").upper()
        callsign = (flight.get("callsign") or "N/A").upper()
        flight_id = flight.get("flight_id") or "N/A"

        alt = flight.get("altitude")
        # altitude is stored in meters; display as feet to match the header.
        alt_str = f"{int(alt * 3.28084):,}" if alt is not None else "N/A"

        spd = flight.get("speed")
        # speed is stored in km/h; display as knots to match the header.
        spd_str = f"{int(spd * 0.539957)}" if spd is not None else "N/A"

        hdg = flight.get("heading")
        hdg_str = f"{int(hdg):03d}°" if hdg is not None else "N/A"

        lat = flight.get("latitude")
        lat_str = f"{lat:.4f}" if lat is not None else "N/A"

        lon = flight.get("longitude")
        lon_str = f"{lon:.4f}" if lon is not None else "N/A"

        alerts = flight.get("active_alerts") or []
        alert_str = "CLEAR"
        if alerts:
            formatted = []
            for a in alerts:
                if isinstance(a, dict):
                    z = a.get("zone")
                    r = a.get("rule")
                    if z and r:
                        formatted.append(f"{z}/{r}")
                    elif r:
                        formatted.append(str(r))
                    elif z:
                        formatted.append(str(z))
                    else:
                        formatted.append("ALERT")
            alert_str = ",".join(sorted(set(formatted))) if formatted else "ALERT"

        status = (
            flight.get("status") or ("LIVE" if flight.get("is_live", True) else "END")
        ).upper()

        last_packet = (
            flight.get("last_packet")
            or flight.get("timestamp")
            or flight.get("start_time")
        )
        if last_packet:
            age = max(0, int(now - last_packet))
            seen_str = f"{age}s ago"
        else:
            seen_str = "live"

        line = f"{icao:<8} {callsign:<10} {flight_id:<16} {alt_str:<10} {spd_str:<11} {hdg_str:<7} {lat_str:<10} {lon_str:<11} {alert_str:<14} {status:<8} {seen_str:<10}"
        lines.append(line)

    return "\n".join(lines)


def run_live_loop(
    live_store: Any,
    interval: float = 1.0,
    once: bool = False,
) -> None:
    """Run live flight display loop."""
    while True:
        live_flights = live_store.get_flights()
        if hasattr(live_store, "update_live"):
            live_store.update_live()

        table = format_dump1090_table(live_flights)

        if not once:
            sys.stdout.write("\033[H\033[J")

        sys.stdout.write("=== PyAerial Live Flight Display ===\n")
        sys.stdout.write(table + "\n")
        sys.stdout.flush()

        if once:
            break
        time.sleep(interval)


def run_live_cmd(
    config_path: str = "config.yaml",
    *,
    mock: bool = False,
    interval: float = 1.0,
    once: bool = False,
) -> None:
    """CLI handler for pyaerial live command."""
    config = load_config(config_path)
    live_store = get_live_store(config, mock=mock)

    try:
        run_live_loop(live_store, interval=interval, once=once)
    except KeyboardInterrupt:
        print("\n[live] Stopped.")
