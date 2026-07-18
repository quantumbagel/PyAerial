"""
FastAPI web portal for live flight tracking.

Reads live flights from Redis and retained historical flights from MongoDB.
Serves a React SPA from pyaerial/static and pushes live updates over WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pymongo
import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pyaerial.calc.aircraft_db import AircraftDB, normalize_photo_url
from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.webapp")

_FLIGHT_STATUS_LIVE = "live"
_STATIC_DIR = Path(__file__).parent / "static"
_LIVE_POLL_INTERVAL = 1.0


def _connect_stores(config_path: str) -> tuple[Config, pymongo.MongoClient, pymongo.database.Database, RedisLiveStore]:
    config = load_config(config_path)
    client = pymongo.MongoClient(config.database.uri)
    if config.database.name:
        db = client.get_database(config.database.name)
    else:
        try:
            db = client.get_default_database()
        except Exception:
            db = client.get_database("pyaerial")
    live_store = RedisLiveStore(config.database.redis_uri)
    return config, client, db, live_store


def _zones_payload(config: Config) -> dict[str, Any]:
    zones = []
    for name, zone in config.zones.items():
        rules = []
        for rule in zone.rules:
            when: dict[str, dict[str, float]] = {}
            for field_name, constraint in rule.when.items():
                entry: dict[str, float] = {}
                if constraint.minimum is not None:
                    entry["min"] = constraint.minimum
                if constraint.maximum is not None:
                    entry["max"] = constraint.maximum
                when[field_name] = entry
            rules.append({
                "name": rule.name,
                "when": when,
                "dwell_seconds": rule.dwell_seconds,
            })
        zones.append({
            "name": name,
            "coordinates": zone.coordinates,
            "rules": rules,
        })
    return {
        "home": {
            "latitude": config.home.latitude,
            "longitude": config.home.longitude,
        },
        "zones": zones,
    }


def _view_param(view: str) -> str:
    view = view.lower()
    return view if view in ("live", "history") else "live"


def _enrich_from_aircraft_db(icao: str, aircraft_db: AircraftDB | None) -> dict[str, str | None]:
    if not aircraft_db:
        return {}
    meta = aircraft_db.lookup_cached(icao)
    if not meta:
        return {}
    return {
        "callsign": meta.get("callsign"),
        "model": meta.get("model"),
        "owner": meta.get("owner"),
        "country": meta.get("country"),
        "aircraft_type": meta.get("typecode"),
        "typecode": meta.get("typecode"),
        "registration": meta.get("registration"),
        "photo_url": normalize_photo_url(meta.get("photo_url")),
        "photo_photographer": meta.get("photo_photographer"),
        "photo_link": meta.get("photo_link"),
    }


def _telemetry_point(doc: dict[str, Any]) -> dict[str, Any]:
    point: dict[str, Any] = {
        "timestamp": doc["timestamp"],
        "altitude": doc.get("altitude"),
        "speed": doc.get("speed", doc.get("horizontal_speed")),
        "heading": doc.get("heading", doc.get("direction")),
    }
    if "latitude" in doc and "longitude" in doc:
        point["latitude"] = doc["latitude"]
        point["longitude"] = doc["longitude"]
    elif "position" in doc:
        position = doc.get("position") or {}
        coords = position.get("coordinates") or [None, None]
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            point["longitude"] = coords[0]
            point["latitude"] = coords[1]
    return point


def _sanitize_for_json(data: Any) -> Any:
    """Convert non-finite floats to null so responses are valid JSON."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {key: _sanitize_for_json(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_sanitize_for_json(value) for value in data]
    if isinstance(data, tuple):
        return [_sanitize_for_json(value) for value in data]
    return data


def _json_response(data: Any) -> JSONResponse:
    return JSONResponse(_sanitize_for_json(data))


def _alert_coords(doc: dict[str, Any]) -> tuple[Any, Any]:
    position = doc.get("position") or {}
    coords = position.get("coordinates") or [None, None]
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None, None
    return coords[1], coords[0]


def _format_alert(doc: dict[str, Any]) -> dict[str, Any]:
    latitude, longitude = _alert_coords(doc)
    alert_id = doc["alert_id"] if "alert_id" in doc else str(doc["_id"])
    return {
        "alert_id": alert_id,
        "flight_id": doc.get("flight_id"),
        "icao": doc.get("icao"),
        "callsign": doc.get("callsign"),
        "zone": doc.get("zone"),
        "level": doc.get("level"),
        "timestamp": doc.get("timestamp"),
        "eta": doc.get("eta"),
        "altitude": doc.get("altitude"),
        "latitude": latitude,
        "longitude": longitude,
    }


def _enrich_flight_summary(summary: dict[str, Any], aircraft_db: AircraftDB | None) -> dict[str, Any]:
    enriched = _enrich_from_aircraft_db(summary.get("icao", ""), aircraft_db)
    return {
        **summary,
        "callsign": summary.get("callsign") or enriched.get("callsign"),
        "model": summary.get("model") or enriched.get("model"),
        "owner": summary.get("owner") or enriched.get("owner"),
        "country": summary.get("country") or enriched.get("country"),
        "aircraft_type": summary.get("aircraft_type") or enriched.get("aircraft_type") or enriched.get("typecode"),
    }


def _flight_summary(doc: dict[str, Any], last_tel: dict[str, Any] | None,
                    aircraft_db: AircraftDB | None) -> dict[str, Any]:
    icao = doc.get("icao", "")
    enriched = _enrich_from_aircraft_db(icao, aircraft_db)
    info = doc.get("info", {})
    lat = lon = alt = speed = heading = timestamp = None
    if last_tel:
        tel = _telemetry_point(last_tel)
        lat = tel.get("latitude")
        lon = tel.get("longitude")
        alt = tel.get("altitude")
        speed = tel.get("speed")
        heading = tel.get("heading")
        timestamp = tel.get("timestamp")
    is_live = doc.get("status") == _FLIGHT_STATUS_LIVE
    return {
        "flight_id": doc["_id"],
        "icao": icao,
        "zone": doc.get("zone"),
        "level": doc.get("level"),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "callsign": doc.get("callsign") or info.get("callsign") or enriched.get("callsign"),
        "model": doc.get("model") or info.get("model") or enriched.get("model"),
        "owner": doc.get("owner") or info.get("owner") or enriched.get("owner"),
        "country": doc.get("country") or info.get("country") or enriched.get("country"),
        "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode") or enriched.get("aircraft_type") or enriched.get("typecode"),
        "latitude": lat,
        "longitude": lon,
        "altitude": alt,
        "speed": speed,
        "heading": heading,
        "is_live": is_live,
        "status": doc.get("status", "completed"),
        "retained": doc.get("retained", False),
        "timestamp": timestamp,
    }


def _get_live_flights(live_store: RedisLiveStore, aircraft_db: AircraftDB | None) -> list[dict[str, Any]]:
    return [
        _enrich_flight_summary(summary, aircraft_db)
        for summary in live_store.get_flights()
    ]


def _get_history_flights(db: pymongo.database.Database, aircraft_db: AircraftDB | None) -> list[dict[str, Any]]:
    flights_col = db.get_collection("flights")
    telemetry_col = db.get_collection("telemetry")
    alerts_col = db.get_collection("alerts")

    completed_cursor = flights_col.find({
        "status": {"$ne": _FLIGHT_STATUS_LIVE},
        "$or": [{"retained": True}, {"retained": {"$exists": False}}],
    }).sort("end_time", -1).limit(100)

    completed_docs = []
    for doc in completed_cursor:
        if doc.get("retained") or alerts_col.count_documents({"flight_id": doc["_id"]}, limit=1):
            completed_docs.append(doc)
        if len(completed_docs) >= 50:
            break

    results = []
    for doc in completed_docs:
        last_tel = telemetry_col.find_one(
            {"flight_id": doc["_id"]},
            sort=[("timestamp", pymongo.DESCENDING)],
        )
        results.append(_flight_summary(doc, last_tel, aircraft_db))
    return results


def _get_live_alerts(live_store: RedisLiveStore, *, since: float = 0.0,
                     flight_id: str | None = None, level: str | None = None,
                     limit: int = 0, skip: int = 0) -> list[dict[str, Any]]:
    alerts = live_store.get_alerts(since=since, flight_id=flight_id, level=level)
    if skip:
        alerts = alerts[skip:]
    if limit:
        alerts = alerts[:limit]
    return [_format_alert(alert) for alert in alerts]


class LiveBroadcaster:
    """Poll Redis and push live updates to connected WebSocket clients."""

    def __init__(self, live_store: RedisLiveStore, aircraft_db: AircraftDB | None):
        self.live_store = live_store
        self.aircraft_db = aircraft_db
        self._clients: dict[WebSocket, float] = {}
        self._task: asyncio.Task | None = None
        self._last_flights_sig: str | None = None
        self._last_alerts_sig: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients[websocket] = 0.0
        await self._send_snapshot(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)

    async def _send_snapshot(self, websocket: WebSocket) -> None:
        flights = _get_live_flights(self.live_store, self.aircraft_db)
        await websocket.send_json({"type": "flights", "flights": _sanitize_for_json(flights)})
        alerts = _get_live_alerts(self.live_store, limit=50)
        await websocket.send_json({"type": "alerts", "alerts": _sanitize_for_json(alerts)})

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._clients:
                    await self._broadcast_tick()
            except Exception:
                log.exception("Live broadcaster tick failed")
            await asyncio.sleep(_LIVE_POLL_INTERVAL)

    async def _broadcast_tick(self) -> None:
        now = time.time()

        flights = _get_live_flights(self.live_store, self.aircraft_db)
        flights_sig = json.dumps(_sanitize_for_json(flights), sort_keys=True, default=str)
        if flights_sig != self._last_flights_sig:
            self._last_flights_sig = flights_sig
            msg = {"type": "flights", "flights": _sanitize_for_json(flights)}
            await self._broadcast(msg)

        alerts = _get_live_alerts(self.live_store, limit=50)
        alerts_sig = json.dumps(_sanitize_for_json(alerts), sort_keys=True, default=str)
        if alerts_sig != self._last_alerts_sig:
            self._last_alerts_sig = alerts_sig
            msg = {"type": "alerts", "alerts": _sanitize_for_json(alerts)}
            await self._broadcast(msg)

        for websocket, since in list(self._clients.items()):
            points = self.live_store.get_live_telemetry(since)
            if points:
                payload = {
                    "type": "telemetry",
                    "telemetry": _sanitize_for_json(points),
                    "timestamp": now,
                }
                try:
                    await websocket.send_json(payload)
                    self._clients[websocket] = now
                except Exception:
                    self.disconnect(websocket)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        for websocket in list(self._clients):
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(websocket)


def create_app(*, config: Config, db: pymongo.database.Database,
               live_store: RedisLiveStore, aircraft_db: AircraftDB | None) -> FastAPI:
    broadcaster = LiveBroadcaster(live_store, aircraft_db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await broadcaster.start()
        yield
        await broadcaster.stop()

    app = FastAPI(title="PyAerial Web Portal", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/flights")
    def api_flights(view: str = Query("live")):
        view = _view_param(view)
        try:
            if view == "live":
                return _json_response(_get_live_flights(live_store, aircraft_db))
            return _json_response(_get_history_flights(db, aircraft_db))
        except Exception as exc:
            raise HTTPException(500, f"Database error: {exc}") from exc

    @app.get("/api/flight")
    def api_flight(flight_id: str = Query(...), view: str = Query("live")):
        view = _view_param(view)
        try:
            if view == "live":
                flight_data = live_store.get_flight(flight_id)
                if not flight_data:
                    raise HTTPException(404, "Flight not found")
                icao = flight_data.get("icao", "")
                enriched = _enrich_from_aircraft_db(icao, aircraft_db)
                flight_data = {
                    **flight_data,
                    "callsign": flight_data.get("callsign") or enriched.get("callsign"),
                    "model": flight_data.get("model") or enriched.get("model"),
                    "owner": flight_data.get("owner") or enriched.get("owner"),
                    "country": flight_data.get("country") or enriched.get("country"),
                    "aircraft_type": flight_data.get("aircraft_type") or enriched.get("typecode"),
                    "registration": flight_data.get("registration") or enriched.get("registration"),
                    "photo_url": enriched.get("photo_url"),
                    "photo_photographer": enriched.get("photo_photographer"),
                    "photo_link": enriched.get("photo_link"),
                }
                return _json_response(flight_data)

            doc = db.get_collection("flights").find_one({"_id": flight_id})
            if not doc:
                raise HTTPException(404, "Flight not found")
            icao = doc.get("icao", "")
            enriched = _enrich_from_aircraft_db(icao, aircraft_db)
            info = doc.get("info", {})
            flight_data = {
                "flight_id": doc["_id"],
                "icao": icao,
                "zone": doc.get("zone"),
                "level": doc.get("level"),
                "start_time": doc.get("start_time"),
                "end_time": doc.get("end_time"),
                "callsign": doc.get("callsign") or info.get("callsign") or enriched.get("callsign"),
                "model": doc.get("model") or info.get("model") or enriched.get("model"),
                "owner": doc.get("owner") or info.get("owner") or enriched.get("owner"),
                "country": doc.get("country") or info.get("country") or enriched.get("country"),
                "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode") or enriched.get("aircraft_type") or enriched.get("typecode"),
                "registration": doc.get("registration") or info.get("registration") or enriched.get("registration"),
                "photo_url": enriched.get("photo_url"),
                "photo_photographer": enriched.get("photo_photographer"),
                "photo_link": enriched.get("photo_link"),
                "raw_messages": doc.get("raw_messages", []),
                "is_live": False,
                "status": doc.get("status", "completed"),
            }
            return _json_response(flight_data)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"Database error: {exc}") from exc

    @app.get("/api/telemetry")
    def api_telemetry(flight_id: str = Query(...), view: str = Query("live"), since: float = 0.0):
        view = _view_param(view)
        try:
            if view == "live":
                points = live_store.get_telemetry(flight_id, since=since)
                return _json_response(points)

            filt: dict[str, Any] = {"flight_id": flight_id}
            if since > 0:
                filt["timestamp"] = {"$gt": since}
            cursor = db.get_collection("telemetry").find(filt).sort("timestamp", 1)
            points = [_telemetry_point(doc) for doc in cursor]
            return _json_response(points)
        except Exception as exc:
            raise HTTPException(500, f"Database error: {exc}") from exc

    @app.get("/api/alerts")
    def api_alerts(
        view: str = Query("live"),
        since: float = 0.0,
        flight_id: str | None = None,
        level: str | None = None,
        limit: int = 0,
        skip: int = 0,
    ):
        view = _view_param(view)
        try:
            if view == "live":
                return _json_response(_get_live_alerts(
                    live_store, since=since, flight_id=flight_id, level=level, limit=limit, skip=skip,
                ))

            filt: dict[str, Any] = {}
            if since:
                filt["timestamp"] = {"$gt": since}
            if flight_id:
                filt["flight_id"] = flight_id
            if level:
                filt["level"] = level

            cursor = db.get_collection("alerts").find(filt).sort("timestamp", -1)
            if skip:
                cursor = cursor.skip(skip)
            if limit:
                cursor = cursor.limit(limit)
            return _json_response([_format_alert(doc) for doc in cursor])
        except Exception as exc:
            raise HTTPException(500, f"Database error: {exc}") from exc

    @app.get("/api/zones")
    def api_zones():
        try:
            return _json_response(_zones_payload(config))
        except Exception as exc:
            raise HTTPException(500, f"Configuration error: {exc}") from exc

    async def handle_ws_request(action: str, params: dict[str, Any]) -> Any:
        view = _view_param(params.get("view", "live"))
        if action == "fetchFlights":
            if view == "live":
                return _get_live_flights(live_store, aircraft_db)
            return _get_history_flights(db, aircraft_db)

        elif action == "fetchFlight":
            flight_id = params.get("flight_id") or params.get("flightId")
            if not flight_id:
                raise ValueError("Missing flight_id")
            if view == "live":
                flight_data = live_store.get_flight(flight_id)
                if not flight_data:
                    return None
                icao = flight_data.get("icao", "")
                enriched = _enrich_from_aircraft_db(icao, aircraft_db)
                return {
                    **flight_data,
                    "callsign": flight_data.get("callsign") or enriched.get("callsign"),
                    "model": flight_data.get("model") or enriched.get("model"),
                    "owner": flight_data.get("owner") or enriched.get("owner"),
                    "country": flight_data.get("country") or enriched.get("country"),
                    "aircraft_type": flight_data.get("aircraft_type") or enriched.get("typecode"),
                    "registration": flight_data.get("registration") or enriched.get("registration"),
                    "photo_url": enriched.get("photo_url"),
                    "photo_photographer": enriched.get("photo_photographer"),
                    "photo_link": enriched.get("photo_link"),
                }
            else:
                doc = db.get_collection("flights").find_one({"_id": flight_id})
                if not doc:
                    return None
                icao = doc.get("icao", "")
                enriched = _enrich_from_aircraft_db(icao, aircraft_db)
                info = doc.get("info", {})
                return {
                    "flight_id": doc["_id"],
                    "icao": icao,
                    "zone": doc.get("zone"),
                    "level": doc.get("level"),
                    "start_time": doc.get("start_time"),
                    "end_time": doc.get("end_time"),
                    "callsign": doc.get("callsign") or info.get("callsign") or enriched.get("callsign"),
                    "model": doc.get("model") or info.get("model") or enriched.get("model"),
                    "owner": doc.get("owner") or info.get("owner") or enriched.get("owner"),
                    "country": doc.get("country") or info.get("country") or enriched.get("country"),
                    "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode") or enriched.get("aircraft_type") or enriched.get("typecode"),
                    "registration": doc.get("registration") or info.get("registration") or enriched.get("registration"),
                    "photo_url": enriched.get("photo_url"),
                    "photo_photographer": enriched.get("photo_photographer"),
                    "photo_link": enriched.get("photo_link"),
                    "raw_messages": doc.get("raw_messages", []),
                    "is_live": False,
                    "status": doc.get("status", "completed"),
                }
 
        elif action == "fetchTelemetry":
            flight_id = params.get("flight_id") or params.get("flightId")
            if not flight_id:
                raise ValueError("Missing flight_id")
            since_val = params.get("since")
            since = float(since_val) if since_val is not None else 0.0
            if view == "live":
                return live_store.get_telemetry(flight_id, since=since)
            filt: dict[str, Any] = {"flight_id": flight_id}
            if since > 0:
                filt["timestamp"] = {"$gt": since}
            cursor = db.get_collection("telemetry").find(filt).sort("timestamp", 1)
            return [_telemetry_point(doc) for doc in cursor]
 
        elif action == "fetchAlerts":
            since_val = params.get("since")
            since = float(since_val) if since_val is not None else 0.0
            flight_id = params.get("flight_id") or params.get("flightId")
            level = params.get("level")
            limit_val = params.get("limit")
            limit = int(limit_val) if limit_val is not None else 0
            skip_val = params.get("skip")
            skip = int(skip_val) if skip_val is not None else 0
            if view == "live":
                return _get_live_alerts(
                    live_store, since=since, flight_id=flight_id, level=level, limit=limit, skip=skip,
                )
            filt: dict[str, Any] = {}
            if since:
                filt["timestamp"] = {"$gt": since}
            if flight_id:
                filt["flight_id"] = flight_id
            if level:
                filt["level"] = level
            cursor = db.get_collection("alerts").find(filt).sort("timestamp", -1)
            if skip:
                cursor = cursor.skip(skip)
            if limit:
                cursor = cursor.limit(limit)
            return [_format_alert(doc) for doc in cursor]

        elif action == "fetchZones":
            return _zones_payload(config)

        else:
            raise ValueError(f"Unknown action: {action}")

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        await broadcaster.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    req = json.loads(data)
                    if isinstance(req, dict) and req.get("type") == "request":
                        req_id = req.get("id")
                        action = req.get("action")
                        params = req.get("params", {})
                        try:
                            res_data = await handle_ws_request(action, params)
                            await websocket.send_json({
                                "type": "response",
                                "id": req_id,
                                "success": True,
                                "data": _sanitize_for_json(res_data)
                            })
                        except Exception as inner_exc:
                            log.error(f"Error executing action {action}: {inner_exc}")
                            await websocket.send_json({
                                "type": "response",
                                "id": req_id,
                                "success": False,
                                "error": str(inner_exc)
                            })
                except Exception as parse_exc:
                    log.error(f"Error parsing WS message: {parse_exc}")
        except WebSocketDisconnect:
            broadcaster.disconnect(websocket)
        except Exception:
            broadcaster.disconnect(websocket)

    assets_dir = _STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    def serve_index():
        index = _STATIC_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(
                503,
                "Frontend not built. Run: cd web && npm install && npm run build",
            )
        return FileResponse(index)

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("ws/"):
            raise HTTPException(404)
        file_path = _STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        index = _STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(
            503,
            "Frontend not built. Run: cd web && npm install && npm run build",
        )

    return app


def run_webapp(config_path: str = "config.yaml", *,
               aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
               host: str = "0.0.0.0", port: int = 10090) -> None:
    config, client, db, live_store = _connect_stores(config_path)
    aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None
    app = create_app(config=config, db=db, live_store=live_store, aircraft_db=aircraft_db)

    print(f"Starting PyAerial web portal on http://localhost:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        print("\nStopping web server...")
    finally:
        client.close()
        live_store.close()
        if aircraft_db:
            aircraft_db.close()
