"""Command handlers for the interactive flight viewer."""

from __future__ import annotations

import json
import time
from typing import Any

from pyaerial.constants import (
    LIVE_ENGINE_TTL_SECONDS,
    STORE_CALC_DATA,
    STORE_INFO,
    STORE_RECV_DATA,
)
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.history import HistoryStore
from pyaerial.view.format import (
    format_duration,
    format_size,
    format_timestamp,
    packet_field_name,
)
from pyaerial.view.live_display import live_empty_message


def _engine_is_live(live_store: Any, now: float | None = None) -> bool:
    getter = getattr(live_store, "engine_seen_at", None)
    seen = getter() if callable(getter) else None
    if not isinstance(seen, (int, float)):
        return False
    return (now if now is not None else time.time()) - seen < LIVE_ENGINE_TTL_SECONDS


def cmd_status(history: HistoryStore | None, live_store: Any = None) -> None:
    if history is not None and history.ping():
        saved_planes = len(history.distinct_icaos())
        saved_flights = history.count_flights()
        total_size = history.data_size()
        history_summary = (
            f"Saved {saved_planes} plane(s) and {saved_flights} flight(s). "
            f"Total data size: {total_size} bytes."
        )
    else:
        history_summary = "Saved history database: disconnected."

    if live_store is not None:
        live_summary = _live_status_line(live_store)
        print(f"{live_summary} {history_summary}")
    else:
        print(history_summary)


def _live_status_line(live_store: Any) -> str:
    ping = getattr(live_store, "ping", None)
    redis_ok = True
    try:
        if callable(ping):
            redis_ok = bool(ping())
    except Exception:
        redis_ok = False
    if not redis_ok:
        return live_empty_message(redis_ok=False)
    try:
        live_flights = live_store.get_flights() or []
    except Exception:
        return "Live store: unavailable."
    if live_flights:
        return f"Live tracking: {len(live_flights)} active flight(s)."
    seen = None
    getter = getattr(live_store, "engine_seen_at", None)
    if callable(getter):
        seen = getter()
    return live_empty_message(redis_ok=True, engine_seen_at=seen)


def cmd_list(
    history: HistoryStore | None,
    parts: list[str],
    aircraft_db: AircraftDB,
    live_store: Any = None,
) -> None:
    if len(parts) < 2:
        print("[err] No argument supplied to command list!")
        return

    arg = parts[1].lower()

    if arg == "planes":
        planes_set: set[str] = set()
        live_planes_set: set[str] = set()

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

        if history is not None:
            planes_set.update(icao.lower() for icao in history.distinct_icaos())

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
        if not _verify_plane(history, plane_id, live_store=live_store):
            return

        flights: list[str] = []

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

        if history is not None:
            flights.extend(
                doc["_id"] for doc in history.flights_for_icao(plane_id)
            )

        print(f"Flights for plane {plane_id} ({len(flights)}): {' '.join(flights)}")

    elif arg == "plane":
        if len(parts) < 3:
            print("[err] list plane requires a plane id")
            return
        plane_id = parts[2].lower()
        if not _verify_plane(history, plane_id, live_store=live_store):
            return

        _display_plane_details(history, live_store, plane_id, aircraft_db)

    else:
        print(f"I don't know the argument {arg!r}!")


def cmd_reset(
    history: HistoryStore | None,
    parts: list[str],
    last_reset: bool = False,
    reset_for: str = "",
    live_store: Any = None,
) -> tuple[bool, str]:
    if len(parts) == 1:
        if not last_reset or reset_for:
            print(
                "[confirmation] Are you sure you want to reset the database? "
                'Run "reset" again to confirm.'
            )
            return True, ""

        history_ok = True
        if history is not None:
            reset = getattr(history, "reset_all", None)
            history_ok = bool(reset()) if callable(reset) else True
            if not history_ok:
                print("[err] History database is disconnected; nothing was reset.")
                return False, ""

        if live_store is not None and hasattr(live_store, "clear_all"):
            if _engine_is_live(live_store):
                print(
                    "[warning] Tracking engine is running; live Redis was not "
                    "cleared (it would be rewritten on the next tick). Stop "
                    "`pyaerial run` first to drop live tracks. History was reset."
                )
                return False, ""
            live_store.clear_all()
            print("[success] Database reset. Dropped all planes and flights.")
            return False, ""

        print("[success] History reset.")
        return False, ""

    target = parts[1].lower()
    if not last_reset or reset_for != target:
        print(
            f'[confirmation] Delete plane {target}? Run "reset {target}" again to confirm.'
        )
        return True, target

    history_ok = True
    if history is not None:
        delete = getattr(history, "delete_icao", None)
        history_ok = bool(delete(target)) if callable(delete) else True
        if not history_ok:
            print(
                f"[err] History database is disconnected; plane {target} was not deleted."
            )
            return False, ""
    if live_store is not None:
        if _engine_is_live(live_store):
            print(
                f"[warning] Tracking engine is running; live track {target} "
                "was not dropped. Stop `pyaerial run` first. History was deleted."
            )
            return False, ""
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


def cmd_dump(
    history: HistoryStore | None,
    parts: list[str],
    aircraft_db: AircraftDB,
    live_store: Any = None,
) -> None:
    if len(parts) < 2:
        print(
            "[err] dump requires a subcommand or plane id "
            "(plane, flight, live, all, aircraft <icao>)"
        )
        return

    arg = parts[1].lower()

    if arg in {"aircraft", "opensky"}:
        if len(parts) < 3:
            print("[err] dump aircraft requires an ICAO id")
            return
        plane = parts[2]
        lookup = None
        if aircraft_db is not None:
            lookup = getattr(aircraft_db, "lookup_cached_fast", None) or getattr(
                aircraft_db, "lookup_cached", None
            )
        record = lookup(plane) if callable(lookup) else None
        print(json.dumps(record, indent=2) if record else "No record found.")
        return

    if arg == "live":
        if live_store is None:
            print("[]")
            return
        ping = getattr(live_store, "ping", None)
        try:
            redis_ok = bool(ping()) if callable(ping) else True
        except Exception:
            redis_ok = False
        if not redis_ok:
            print(live_empty_message(redis_ok=False))
            return
        live_flights = live_store.get_flights() or []
        if not live_flights:
            seen = None
            getter = getattr(live_store, "engine_seen_at", None)
            if callable(getter):
                seen = getter()
            print(live_empty_message(redis_ok=True, engine_seen_at=seen))
            return
        print(json.dumps(live_flights, indent=2, default=str))
        return

    if arg == "all":
        print("Dumping all data (this may take a while)...")
        data = {}
        if live_store is not None:
            try:
                live_flights = live_store.get_flights()
                for lf in live_flights:
                    pid = lf.get("icao", "").lower()
                    if pid:
                        data[pid] = _dump_plane(history, pid, live_store=live_store)
            except Exception:
                pass

        if history is not None:
            for plane_id in history.distinct_icaos():
                if plane_id.lower() not in data:
                    data[plane_id.lower()] = _dump_plane(
                        history, plane_id.lower(), live_store=live_store
                    )

        print(json.dumps(data, indent=2, default=str))
        return

    if arg == "plane" or (
        len(parts) == 2
        and arg not in {"flight", "all", "aircraft", "opensky", "live"}
    ):
        plane_id = parts[2] if arg == "plane" else parts[1]
        if not _verify_plane(history, plane_id, live_store=live_store):
            return
        print(
            json.dumps(
                {plane_id: _dump_plane(history, plane_id, live_store=live_store)},
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
            history, plane_id, live_store=live_store
        ) or not _verify_flight(history, plane_id, flight_id, live_store=live_store):
            return
        print(
            json.dumps(
                {
                    plane_id: {
                        flight_id: _dump_flight(
                            history, flight_id, live_store=live_store
                        )
                    }
                },
                indent=2,
                default=str,
            )
        )
        return

    print(f"[err] Unknown dump subcommand {arg!r}")


def _verify_plane(
    history: HistoryStore | None, plane_id: str, live_store: Any = None
) -> bool:
    plane_id_lower = plane_id.lower()

    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            if any(f.get("icao", "").lower() == plane_id_lower for f in live_flights):
                return True
        except Exception:
            pass

    if history is not None and history.has_icao(plane_id_lower):
        return True

    print(f"I don't know the plane {plane_id!r}!")
    return False


def _verify_flight(
    history: HistoryStore | None,
    plane_id: str,
    flight_id: str,
    live_store: Any = None,
) -> bool:
    plane_id_lower = plane_id.lower()

    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            if any(f.get("flight_id") == flight_id for f in live_flights):
                return True
        except Exception:
            pass

    if history is not None and history.has_flight(plane_id_lower, flight_id):
        return True

    print(f"I don't know the flight id {flight_id!r}")
    return False


def _display_plane_details(
    history: HistoryStore | None,
    live_store: Any,
    plane_id: str,
    aircraft_db: AircraftDB,
) -> None:
    lookup = None
    if aircraft_db:
        lookup = getattr(aircraft_db, "lookup_cached_fast", None) or getattr(
            aircraft_db, "lookup_cached", None
        )
    meta = lookup(plane_id) if callable(lookup) else {}
    if not meta:
        meta = {}
    callsign = meta.get("callsign") or "n/a"
    category = meta.get("category") or meta.get("aircraft_type") or "n/a"

    saved_flights: list[dict] = []
    if history is not None:
        saved_flights = history.flights_for_icao(plane_id)

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

    if live_flight and live_flight.get("callsign"):
        callsign = live_flight["callsign"]

    first_seen: float | None = None
    last_seen: float | None = None
    total_bytes = 0
    recent_flight_packets: dict[str, int] = {}
    overall_packets: dict[str, int] = {}
    most_recent_duration = 0.0
    most_recent_status = "completed"

    for fdoc in saved_flights:
        fid = fdoc["_id"]
        tel_docs = history.get_telemetry(fid) if history is not None else []

        for tdoc in tel_docs:
            ts = tdoc.get("timestamp")
            if ts is not None:
                if first_seen is None or ts < first_seen:
                    first_seen = ts
                if last_seen is None or ts > last_seen:
                    last_seen = ts

            total_bytes += 128
            for k in (
                "latitude",
                "longitude",
                "altitude",
                "speed",
                "heading",
                "vertical_speed",
            ):
                if k in tdoc:
                    name = packet_field_name(k)
                    overall_packets[name] = overall_packets.get(name, 0) + 1

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
        if history is not None:
            tel_docs = history.get_telemetry(fid)
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
                        name = packet_field_name(k)
                        recent_flight_packets[name] = (
                            recent_flight_packets.get(name, 0) + 1
                        )

    if live_flight:
        st = live_flight.get("start_time") or time.time()
        now = time.time()
        most_recent_duration = max(0.0, now - st)
        most_recent_status = "ongoing"
        if first_seen is None or st < first_seen:
            first_seen = st
        last_seen = now

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
                    name = packet_field_name(k)
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
    print(f"Storage: {format_size(total_bytes)}")
    print(f"Plane Category: {category}")
    print(f"First Discovered: {format_timestamp(first_seen)}")
    print(f"Last Updated: {format_timestamp(last_seen)}")
    print(
        f"Most recent flight duration: {format_duration(most_recent_duration)} ({most_recent_status})"
    )
    print("Packet breakdown (most recent flight):")
    for pkt_name, count in recent_flight_packets.items():
        print(f"{count} {pkt_name}")
    print("\nPacket breakdown (overall):")
    for pkt_name, count in overall_packets.items():
        print(f"{count} {pkt_name}")
    print(f'\nTo display this plane\'s raw data, run "dump {plane_id}"')
    print(f'To delete this plane, run "reset {plane_id}"')


def _dump_plane(
    history: HistoryStore | None, plane_id: str, live_store: Any = None
) -> dict:
    plane_id_lower = plane_id.lower()
    results: dict = {}

    if live_store is not None:
        try:
            live_flights = live_store.get_flights()
            for lf in live_flights:
                if lf.get("icao", "").lower() == plane_id_lower:
                    fid = lf.get("flight_id", f"{plane_id_lower}-live")
                    results[fid] = _dump_flight(
                        history, fid, live_store=live_store
                    )
        except Exception:
            pass

    if history is not None:
        for doc in history.flights_for_icao(plane_id_lower):
            fid = doc["_id"]
            if fid not in results:
                results[fid] = _dump_flight(history, fid, live_store=live_store)

    return results


def _dump_flight(
    history: HistoryStore | None,
    flight_id: str,
    live_store: Any = None,
) -> dict:
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

    if history is None:
        return {}

    flight_doc = history.get_flight(flight_id)
    if not flight_doc:
        return {}

    series_data = {}
    for doc in history.get_telemetry(flight_id):
        t = doc["timestamp"]
        for k, v in doc.items():
            if k in ("_id", "flight_id", "icao", "timestamp"):
                continue
            series_data.setdefault(k, []).append([t, v])

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

    alerts = history.get_alerts(flight_id=flight_id)
    zone = None
    rule = None
    if alerts:
        zone = alerts[0].get("zone")
        rule = alerts[0].get("rule")
    info_doc = {
        "category": "info",
        "zone": zone,
        "rule": rule,
        "start_time": flight_doc.get("start_time"),
        "end_time": flight_doc.get("end_time"),
    }
    if "info" in flight_doc:
        info_doc.update(flight_doc["info"])
    if "internal" in flight_doc:
        info_doc.update(flight_doc["internal"])

    result[STORE_INFO] = info_doc
    return result
