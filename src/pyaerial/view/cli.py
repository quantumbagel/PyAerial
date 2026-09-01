"""
Interactive flight viewer and live telemetry browser for PyAerial.

Supports browsing saved flights in MongoDB as well as live flights from Redis
or MockStore, with a dump1090-like live flight display mode.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from typing import Any

import pymongo

from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config import load_config
from pyaerial.constants import (
    DEFAULT_AIRCRAFT_DB,
    STORE_INFO,
    STORE_CALC_DATA,
    STORE_RECV_DATA,
)
from pyaerial.view.live_display import run_live_loop
from pyaerial.view.store import get_live_store

log = logging.getLogger("pyaerial.view")

HELP_TEXT = """
PyAerial Flight Viewer

help      - display this help text
about     - info about PyAerial
exit      - close this terminal
reset     - reset database or individual planes (requires confirmation)
list      - show summarized information (planes, flights, plane <id>)
dump      - show raw information (plane <id>, flight <id>, live, all, opensky <id>)
status    - database and live stream summary
live      - start live flight display
""".strip()


_VIEW_DB_NAME: str | None = None


def _get_mongo_db(
    client: pymongo.MongoClient | None,
) -> pymongo.database.Database | None:
    if client is None:
        return None
    try:
        if hasattr(client, "admin"):
            client.admin.command("ping")
        if _VIEW_DB_NAME:
            return client.get_database(_VIEW_DB_NAME)
        try:
            return client.get_default_database()
        except Exception:
            return client.get_database("pyaerial")
    except Exception:
        return None


def run_view(
    config_path: str = "config.yaml",
    *,
    aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
    mock: bool = False,
) -> None:
    """Run interactive flight viewer command-line session."""
    global _VIEW_DB_NAME
    config = load_config(config_path)
    _VIEW_DB_NAME = config.database.name
    aircraft_db = AircraftDB(aircraft_db_path)
    live_store = get_live_store(config, mock=mock, aircraft_db=aircraft_db)

    client: pymongo.MongoClient | None = None
    try:
        client = pymongo.MongoClient(config.database.uri, serverSelectionTimeoutMS=2000)
    except Exception:
        client = None

    print("Ready for user input.")
    last_reset = False
    reset_for = ""

    while True:
        try:
            prompt = input("> ")
        except (KeyboardInterrupt, EOFError):
            print("\nlogout")
            return

        parts = prompt.split()
        if not parts:
            continue

        verb = parts[0].lower()
        if verb not in {
            "about",
            "status",
            "plane",
            "list",
            "history",
            "help",
            "dump",
            "reset",
            "exit",
            "live",
        }:
            print(f"[err] Invalid verb: {verb}")
            last_reset = False
            continue

        if verb in {"plane", "history"}:
            print(f"[err] '{verb}' is not a command. Try 'list planes' or 'help'.")
            last_reset = False
            continue

        if verb == "about":
            print(
                "PyAerial by Julian Reder (quantumbagel). "
                "Source: https://github.com/quantumbagel/PyAerial"
            )
        elif verb == "status":
            _cmd_status(client, live_store=live_store)
        elif verb == "list":
            _cmd_list(client, parts, aircraft_db, live_store=live_store)
        elif verb == "reset":
            last_reset, reset_for = _cmd_reset(
                client, parts, last_reset, reset_for, live_store=live_store
            )
        elif verb == "exit":
            print("logout")
            return
        elif verb == "help":
            print(HELP_TEXT)
        elif verb == "dump":
            _cmd_dump(client, parts, aircraft_db, live_store=live_store)
        elif verb == "live":
            try:
                run_live_loop(live_store, aircraft_db=aircraft_db)
            except KeyboardInterrupt:
                print("\n[live] Stopped.")

        if verb != "reset":
            last_reset = False


def _cmd_status(client: pymongo.MongoClient | None, live_store: Any = None) -> None:
    db = _get_mongo_db(client)

    # Saved MongoDB stats
    if db is not None:
        try:
            saved_planes = len(db.get_collection("flights").distinct("icao"))
            saved_flights = db.get_collection("flights").count_documents({})
            stats = db.command("dbStats")
            total_size = stats.get("dataSize", 0)
            mongo_summary = f"Saved {saved_planes} plane(s) and {saved_flights} flight(s). Total data size: {total_size} bytes."
        except Exception:
            mongo_summary = "Saved MongoDB data: unavailable."
    else:
        mongo_summary = "Saved MongoDB database: disconnected."

    # Live store stats
    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            live_summary = f"Live tracking: {len(live_flights)} active flight(s)."
        except Exception:
            live_summary = "Live store: unavailable."
        print(f"{live_summary} {mongo_summary}")
    else:
        print(mongo_summary)


def _verify_plane(
    client: pymongo.MongoClient | None, plane_id: str, live_store: Any = None
) -> bool:
    plane_id_lower = plane_id.lower()

    # Check live store first
    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            if any(f.get("icao", "").lower() == plane_id_lower for f in live_flights):
                return True
        except Exception:
            pass

    # Check MongoDB
    db = _get_mongo_db(client)
    if db is not None:
        record = db.get_collection("flights").find_one({"icao": plane_id_lower})
        if record is not None:
            return True

    print(f"I don't know the plane {plane_id!r}!")
    return False


def _verify_flight(
    client: pymongo.MongoClient | None,
    plane_id: str,
    flight_id: str,
    live_store: Any = None,
) -> bool:
    plane_id_lower = plane_id.lower()

    # Check live store
    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            if any(f.get("flight_id") == flight_id for f in live_flights):
                return True
        except Exception:
            pass

    # Check MongoDB
    db = _get_mongo_db(client)
    if db is not None:
        record = db.get_collection("flights").find_one(
            {"icao": plane_id_lower, "_id": flight_id}
        )
        if record is not None:
            return True

    print(f"I don't know the flight id {flight_id!r}")
    return False


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}b"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}kb"
    else:
        return f"{size_bytes / (1024 * 1024):.2f}mb"


def _format_timestamp(ts: float | None) -> str:
    if not ts:
        return "n/a"
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%B %d, %Y %I:%M %p (UTC)")


def _format_duration(seconds: float) -> str:
    total_sec = int(round(seconds))
    mins, secs = divmod(total_sec, 60)
    hours, mins = divmod(mins, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins > 0 or hours > 0:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


def _cmd_list(
    client: pymongo.MongoClient | None,
    parts: list[str],
    aircraft_db: AircraftDB,
    live_store: Any = None,
) -> None:
    if len(parts) < 2:
        print("[err] No argument supplied to command list!")
        return

    db = _get_mongo_db(client)
    arg = parts[1].lower()

    if arg == "planes":
        planes_set: set[str] = set()
        live_planes_set: set[str] = set()

        # Get live planes
        if live_store is not None:
            try:
                live_flights = live_store.get_flights()
                for lf in live_flights:
                    icao = lf.get("icao", "").lower()
                    if icao:
                        live_planes_set.add(icao)
                        planes_set.add(icao)
            except Exception:
                pass

        # Get saved planes from mongo
        if db is not None:
            try:
                distinct_mongo = db.get_collection("flights").distinct("icao")
                planes_set.update(d.lower() for d in distinct_mongo)
            except Exception:
                pass

        formatted_planes = []
        for p in sorted(planes_set):
            if p in live_planes_set:
                formatted_planes.append(f"{p}(live)")
            else:
                formatted_planes.append(p)

        print(f"Planes ({len(planes_set)}): {' '.join(formatted_planes)}")

    elif arg == "flights":
        if len(parts) < 3:
            print("[err] list flights requires a plane id")
            return
        plane_id = parts[2].lower()
        if not _verify_plane(client, plane_id, live_store=live_store):
            return

        flights: list[str] = []

        # Live flight check
        if live_store is not None:
            try:
                live_flights = live_store.get_flights()
                for lf in live_flights:
                    if lf.get("icao", "").lower() == plane_id:
                        fid = lf.get("flight_id")
                        if fid:
                            flights.append(f"{fid} (live)")
            except Exception:
                pass

        # Saved flights check
        if db is not None:
            try:
                cursor = db.get_collection("flights").find(
                    {"icao": plane_id}, {"_id": 1}
                )
                flights.extend(doc["_id"] for doc in cursor)
            except Exception:
                pass

        print(f"Flights for plane {plane_id} ({len(flights)}): {' '.join(flights)}")

    elif arg == "plane":
        if len(parts) < 3:
            print("[err] list plane requires a plane id")
            return
        plane_id = parts[2].lower()
        if not _verify_plane(client, plane_id, live_store=live_store):
            return

        _display_plane_details(client, live_store, plane_id, aircraft_db)

    else:
        print(f"I don't know the argument {arg!r}!")


def _display_plane_details(
    client: pymongo.MongoClient | None,
    live_store: Any,
    plane_id: str,
    aircraft_db: AircraftDB,
) -> None:
    """Format and print detailed plane statistics according to statviewer_formatting specification."""
    db = _get_mongo_db(client)

    # 1. Lookup aircraft metadata
    meta = aircraft_db.lookup_cached(plane_id) if aircraft_db else {}
    if not meta:
        meta = {}
    callsign = meta.get("callsign") or "n/a"
    meta.get("model") or "n/a"
    category = meta.get("category") or meta.get("aircraft_type") or "n/a"

    # 2. Gather flights & telemetry
    saved_flights: list[dict] = []
    if db is not None:
        try:
            saved_flights = list(db.get_collection("flights").find({"icao": plane_id}))
        except Exception:
            pass

    live_flight: dict | None = None
    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            for lf in live_flights:
                if lf.get("icao", "").lower() == plane_id:
                    live_flight = lf
                    break
        except Exception:
            pass

    total_flights_count = len(saved_flights) + (1 if live_flight else 0)

    # If callsign in live flight, override n/a
    if live_flight and live_flight.get("callsign"):
        callsign = live_flight["callsign"]

    # Compute timestamps, storage size, packet breakdowns
    first_seen: float | None = None
    last_seen: float | None = None
    total_bytes = 0
    recent_flight_packets: dict[str, int] = {}
    overall_packets: dict[str, int] = {}
    most_recent_duration = 0.0
    most_recent_status = "completed"

    # Process saved mongo flights
    for fdoc in saved_flights:
        fid = fdoc["_id"]
        tel_docs = []
        if db is not None:
            tel_docs = list(db.get_collection("telemetry").find({"flight_id": fid}))

        for tdoc in tel_docs:
            ts = tdoc.get("timestamp")
            if ts is not None:
                if first_seen is None or ts < first_seen:
                    first_seen = ts
                if last_seen is None or ts > last_seen:
                    last_seen = ts

            total_bytes += 128  # avg estimated doc size
            for k in (
                "latitude",
                "longitude",
                "altitude",
                "speed",
                "heading",
                "vertical_speed",
            ):
                if k in tdoc:
                    name = _packet_field_name(k)
                    overall_packets[name] = overall_packets.get(name, 0) + 1

    # Most recent flight info from saved
    if saved_flights and not live_flight:
        latest_flight = max(
            saved_flights,
            key=lambda f: (
                f.get("start_time") or f.get("internal", {}).get("first_packet") or 0
            ),
        )
        fid = latest_flight["_id"]
        st = latest_flight.get("start_time") or latest_flight.get("internal", {}).get(
            "first_packet"
        )
        et = latest_flight.get("end_time") or latest_flight.get("internal", {}).get(
            "most_recent_packet"
        )
        if st and et:
            most_recent_duration = max(0.0, et - st)
        most_recent_status = "completed"
        if db is not None:
            tel_docs = list(db.get_collection("telemetry").find({"flight_id": fid}))
            for tdoc in tel_docs:
                for k in (
                    "latitude",
                    "longitude",
                    "altitude",
                    "speed",
                    "heading",
                    "vertical_speed",
                ):
                    if k in tdoc:
                        name = _packet_field_name(k)
                        recent_flight_packets[name] = (
                            recent_flight_packets.get(name, 0) + 1
                        )

    # Process live flight
    if live_flight:
        st = live_flight.get("start_time") or time.time()
        now = time.time()
        most_recent_duration = max(0.0, now - st)
        most_recent_status = "ongoing"
        if first_seen is None or st < first_seen:
            first_seen = st
        last_seen = now

        # Telemetry points for live flight
        fid = live_flight.get("flight_id")
        live_tels = []
        if fid and hasattr(live_store, "get_telemetry"):
            try:
                live_tels = live_store.get_telemetry(fid)
            except Exception:
                pass

        for p in live_tels:
            total_bytes += 64
            for k in ("latitude", "longitude", "altitude", "speed", "heading"):
                if k in p:
                    name = _packet_field_name(k)
                    recent_flight_packets[name] = recent_flight_packets.get(name, 0) + 1
                    overall_packets[name] = overall_packets.get(name, 0) + 1

    if not recent_flight_packets:
        recent_flight_packets = {"Information Packet": 1}
    if not overall_packets:
        overall_packets = dict(recent_flight_packets)

    print(f"Plane: {plane_id}")
    print(f"Callsign: {callsign}")
    print(
        f"Flown: {total_flights_count} flight{'s' if total_flights_count != 1 else ''}"
    )
    print(f"Storage: {_format_size(total_bytes)}")
    print(f"Plane Category: {category}")
    print(f"First Discovered: {_format_timestamp(first_seen)}")
    print(f"Last Updated: {_format_timestamp(last_seen)}")
    print(
        f"Most recent flight duration: {_format_duration(most_recent_duration)} ({most_recent_status})"
    )
    print("Packet breakdown (most recent flight):")
    for pkt_name, count in recent_flight_packets.items():
        print(f"{count} {pkt_name}")
    print("\nPacket breakdown (overall):")
    for pkt_name, count in overall_packets.items():
        print(f"{count} {pkt_name}")
    print(f'\nTo display this plane\'s raw data, run "dump {plane_id}"')
    print(f'To delete this plane, run "reset {plane_id}"')


def _packet_field_name(field: str) -> str:
    mapping = {
        "latitude": "Latitude/Longitude",
        "longitude": "Latitude/Longitude",
        "altitude": "Altitude",
        "speed": "Speeds",
        "heading": "Velocities",
        "vertical_speed": "Velocities",
    }
    return mapping.get(field, field.capitalize())


def _cmd_reset(
    client: pymongo.MongoClient | None,
    parts: list[str],
    last_reset: bool = False,
    reset_for: str = "",
    live_store: Any = None,
) -> tuple[bool, str]:
    db = _get_mongo_db(client)

    if len(parts) == 1:
        if not last_reset or reset_for:
            print(
                "[confirmation] Are you sure you want to reset the database? "
                'Run "reset" again to confirm.'
            )
            return True, ""

        if db is not None:
            db.drop_collection("flights")
            db.drop_collection("telemetry")
            db.drop_collection("alerts")

            db.get_collection("flights").create_index([("icao", pymongo.ASCENDING)])
            db.get_collection("flights").create_index([("status", pymongo.ASCENDING)])
            db.get_collection("telemetry").create_index(
                [("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)]
            )
            db.get_collection("telemetry").create_index(
                [("icao", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)]
            )
            db.get_collection("telemetry").create_index(
                [("position", pymongo.GEOSPHERE)]
            )
            db.get_collection("alerts").create_index(
                [("timestamp", pymongo.DESCENDING)]
            )
            db.get_collection("alerts").create_index(
                [("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)]
            )

        if live_store is not None and hasattr(live_store, "clear_all"):
            live_store.clear_all()

        print("[success] Database reset. Dropped all planes and flights.")
        return False, ""

    target = parts[1].lower()
    if not last_reset or reset_for != target:
        print(
            f'[confirmation] Delete plane {target}? Run "reset {target}" again to confirm.'
        )
        return True, target

    if db is not None:
        db.get_collection("flights").delete_many({"icao": target})
        db.get_collection("telemetry").delete_many({"icao": target})
        db.get_collection("alerts").delete_many({"icao": target})
    if live_store is not None:
        flight_ids = []
        if hasattr(live_store, "get_flights"):
            for flight in live_store.get_flights() or []:
                if str(flight.get("icao", "")).lower() == target:
                    flight_ids.append(flight.get("flight_id"))
        for flight_id in flight_ids:
            if flight_id and hasattr(live_store, "pop_flight"):
                live_store.pop_flight(flight_id)
    print(f"[success] Dropped plane {target}.")
    return False, ""


def _cmd_dump(
    client: pymongo.MongoClient | None,
    parts: list[str],
    aircraft_db: AircraftDB,
    live_store: Any = None,
) -> None:
    if len(parts) < 2:
        print(
            "[err] dump requires a subcommand or plane id (plane, flight, live, all, opensky)"
        )
        return

    db = _get_mongo_db(client)
    arg = parts[1].lower()

    if arg == "opensky":
        if len(parts) < 3:
            print("[err] dump opensky requires a plane id")
            return
        plane = parts[2]
        record = aircraft_db.lookup_cached(plane) if aircraft_db else None
        print(json.dumps(record, indent=2) if record else "No record found.")
        return

    if arg == "live":
        live_flights = []
        if live_store is not None:
            live_flights = live_store.get_flights()
        print(json.dumps(live_flights, indent=2, default=str))
        return

    if arg == "all":
        print("Dumping all data (this may take a while)...")
        data = {}
        # Live flights
        if live_store is not None:
            try:
                live_flights = live_store.get_flights()
                for lf in live_flights:
                    pid = lf.get("icao", "").lower()
                    if pid:
                        data[pid] = _dump_plane(client, pid, live_store=live_store)
            except Exception:
                pass

        # Mongo flights
        if db is not None:
            try:
                distinct_planes = db.get_collection("flights").distinct("icao")
                for plane_id in distinct_planes:
                    if plane_id.lower() not in data:
                        data[plane_id.lower()] = _dump_plane(
                            client, plane_id.lower(), live_store=live_store
                        )
            except Exception:
                pass

        print(json.dumps(data, indent=2, default=str))
        return

    # Check if arg is 'plane' or a direct plane_id (e.g. 'dump a3809r8s')
    if arg == "plane" or (
        len(parts) == 2 and arg not in {"flight", "all", "opensky", "live"}
    ):
        plane_id = parts[2] if arg == "plane" else parts[1]
        if not _verify_plane(client, plane_id, live_store=live_store):
            return
        print(
            json.dumps(
                {plane_id: _dump_plane(client, plane_id, live_store=live_store)},
                indent=2,
                default=str,
            )
        )
        return

    if arg == "flight":
        if len(parts) < 4:
            print("[err] dump flight requires plane id and flight id")
            return
        plane_id, flight_id = parts[2], parts[3]
        if not _verify_plane(
            client, plane_id, live_store=live_store
        ) or not _verify_flight(client, plane_id, flight_id, live_store=live_store):
            return
        print(
            json.dumps(
                {
                    plane_id: {
                        flight_id: _dump_flight(
                            client, plane_id, flight_id, live_store=live_store
                        )
                    }
                },
                indent=2,
                default=str,
            )
        )
        return

    print(f"[err] Unknown dump subcommand {arg!r}")


def _dump_plane(
    client: pymongo.MongoClient | None, plane_id: str, live_store: Any = None
) -> dict:
    db = _get_mongo_db(client)
    plane_id_lower = plane_id.lower()
    results: dict = {}

    # Check live store
    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            for lf in live_flights:
                if lf.get("icao", "").lower() == plane_id_lower:
                    fid = lf.get("flight_id", f"{plane_id_lower}-live")
                    results[fid] = _dump_flight(
                        client, plane_id_lower, fid, live_store=live_store
                    )
        except Exception:
            pass

    # Check mongo
    if db is not None:
        try:
            cursor = db.get_collection("flights").find(
                {"icao": plane_id_lower}, {"_id": 1}
            )
            for doc in cursor:
                fid = doc["_id"]
                if fid not in results:
                    results[fid] = _dump_flight(
                        client, plane_id_lower, fid, live_store=live_store
                    )
        except Exception:
            pass

    return results


def _dump_flight(
    client: pymongo.MongoClient | None,
    plane_id: str,
    flight_id: str,
    live_store: Any = None,
) -> dict:
    db = _get_mongo_db(client)

    # First check live store
    if live_store is not None:
        try:
            live_flight = (
                live_store.get_flight(flight_id)
                if hasattr(live_store, "get_flight")
                else None
            )
            if not live_flight:
                live_flights = live_store.get_flights()
                live_flight = next(
                    (f for f in live_flights if f.get("flight_id") == flight_id), None
                )

            if live_flight:
                tels = (
                    live_store.get_telemetry(flight_id)
                    if hasattr(live_store, "get_telemetry")
                    else []
                )
                series_data: dict[str, list] = {}
                for tdoc in tels:
                    ts = tdoc.get("timestamp", time.time())
                    for k, v in tdoc.items():
                        if k in ("_id", "flight_id", "icao", "timestamp"):
                            continue
                        series_data.setdefault(k, []).append([ts, v])

                res: dict = {}
                for k, points in series_data.items():
                    cat = (
                        STORE_RECV_DATA
                        if k in ("latitude", "longitude", "altitude", "vertical_speed")
                        else STORE_CALC_DATA
                    )
                    res.setdefault(cat, {})[k] = {
                        "category": cat,
                        "type": k,
                        "data": points,
                    }

                res[STORE_INFO] = {
                    "category": "info",
                    "callsign": live_flight.get("callsign"),
                    "status": "live",
                    "model": live_flight.get("model"),
                    "owner": live_flight.get("owner"),
                }
                return res
        except Exception:
            pass

    # Fallback to MongoDB
    if db is None:
        return {}

    flight_doc = db.get_collection("flights").find_one({"_id": flight_id})
    if not flight_doc:
        return {}

    telemetry_cursor = (
        db.get_collection("telemetry")
        .find({"flight_id": flight_id})
        .sort("timestamp", pymongo.ASCENDING)
    )

    series_data = {}
    for doc in telemetry_cursor:
        t = doc["timestamp"]
        has_pos = False
        if "position" in doc and doc["position"].get("type") == "Point":
            coords = doc["position"].get("coordinates", [])
            if len(coords) == 2:
                series_data.setdefault("longitude", []).append([t, coords[0]])
                series_data.setdefault("latitude", []).append([t, coords[1]])
                has_pos = True

        for k, v in doc.items():
            if k in ("_id", "flight_id", "icao", "timestamp", "position"):
                continue
            if has_pos and k in ("latitude", "longitude"):
                continue
            field = k
            series_data.setdefault(field, []).append([t, v])

    result: dict = {}
    for field, data_points in series_data.items():
        if field in ("latitude", "longitude", "altitude", "vertical_speed"):
            category = STORE_RECV_DATA
        else:
            category = STORE_CALC_DATA

        result.setdefault(category, {})
        result[category][field] = {
            "category": category,
            "type": field,
            "data": data_points,
        }

    info_doc = {
        "category": "info",
        "zone": flight_doc.get("zone"),
        "rule": flight_doc.get("rule"),
    }
    if "info" in flight_doc:
        info_doc.update(flight_doc["info"])
    if "internal" in flight_doc:
        info_doc.update(flight_doc["internal"])

    result[STORE_INFO] = info_doc
    return result
