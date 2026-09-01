"""
FastAPI web portal for live flight tracking.

Reads live flights from Redis and retained historical flights from MongoDB.
Serves a React SPA from pyaerial/static and pushes live updates over WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pymongo
import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.connect import connect_stores
from pyaerial.api.payloads import app_config_payload, sanitize_for_json, zones_payload
from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_live_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.api.ws import handle_ws_request
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.webapp")

_STATIC_DIR = Path(__file__).parent / "static"
_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
_FRONTEND_HINT = "Frontend not built. Run: scripts/build_web.sh (or: cd web && npm install && npm run build)"


def _origin_allowed(origin: str | None, host_header: str | None) -> bool:
    if not origin:
        return True
    if _LOCAL_ORIGIN.match(origin):
        return True
    parsed = urlparse(origin)
    request_host = (host_header or "").split(":")[0].lower()
    origin_host = (parsed.hostname or "").lower()
    return bool(origin_host) and origin_host == request_host


def _token_ok(config: Config, token: str | None) -> bool:
    expected = config.web.token
    if not expected:
        return True
    return token == expected


def _safe_static_path(full_path: str) -> Path | None:
    """Resolve a path under the static directory, rejecting traversal attempts."""
    if not full_path:
        return None
    candidate = (_STATIC_DIR / full_path).resolve()
    static_root = _STATIC_DIR.resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        return None
    return candidate


def create_app(
    *,
    config: Config,
    db: pymongo.database.Database | None = None,
    live_store: RedisLiveStore | None = None,
    aircraft_db: AircraftDB | None = None,
) -> FastAPI:
    broadcaster = LiveBroadcaster(live_store, aircraft_db, db=db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await broadcaster.start()
        yield
        await broadcaster.stop()

    app = FastAPI(title="PyAerial Web Portal", lifespan=lifespan)
    app.state.db = db
    app.state.live_store = live_store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )

    def ws_request_handler(action: str, params: dict[str, Any]) -> Any:
        return handle_ws_request(
            action,
            params,
            config=config,
            db=db,
            live_store=live_store,
            aircraft_db=aircraft_db,
        )

    def _check_http_token(token: str | None) -> None:
        if not _token_ok(config, token):
            raise HTTPException(401, "Unauthorized")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        redis_ok = True
        mongo_ok = True
        if live_store is not None and not getattr(live_store, "memory_only", False):
            redis_ok = bool(live_store._ensure_connected())
        if db is not None:
            try:
                db.client.admin.command("ping")
            except Exception:
                mongo_ok = False
        status = "ok" if redis_ok and mongo_ok else "degraded"
        code = 200 if redis_ok else 503
        return JSONResponse(
            {"status": status, "redis": redis_ok, "mongo": mongo_ok},
            status_code=code,
        )

    @app.get("/api/flights")
    def api_flights(
        view: str = "live",
        skip: int = 0,
        limit: int = 50,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
        token: str | None = Query(default=None),
    ):
        _check_http_token(token)
        if view == "live":
            return sanitize_for_json(get_live_flights(live_store, aircraft_db))
        return sanitize_for_json(
            get_history_flights(
                db,
                aircraft_db,
                skip=skip,
                limit=limit,
                q=q,
                since=since,
                until=until,
            )
        )

    @app.get("/api/flights/{flight_id}")
    def api_flight(
        flight_id: str,
        view: str = "live",
        token: str | None = Query(default=None),
    ):
        _check_http_token(token)
        detail = get_flight_detail(
            flight_id,
            view,
            live_store=live_store,
            db=db,
            aircraft_db=aircraft_db,
        )
        if detail is None:
            raise HTTPException(404, "Flight not found")
        return sanitize_for_json(detail)

    @app.get("/api/telemetry")
    def api_telemetry(
        flightId: str,
        view: str = "live",
        since: float = 0.0,
        token: str | None = Query(default=None),
    ):
        _check_http_token(token)
        return sanitize_for_json(
            get_telemetry(
                flightId, view, since, live_store=live_store, db=db
            )
        )

    @app.get("/api/alerts")
    def api_alerts(
        view: str = "live",
        since: float = 0.0,
        flightId: str | None = None,
        rule: str | None = None,
        limit: int = 0,
        skip: int = 0,
        active_only: bool | None = None,
        token: str | None = Query(default=None),
    ):
        _check_http_token(token)
        return sanitize_for_json(
            get_alerts(
                view,
                since=since,
                flight_id=flightId,
                rule=rule,
                limit=limit,
                skip=skip,
                live_store=live_store,
                db=db,
                active_only=active_only,
            )
        )

    @app.get("/api/stats")
    def api_stats(token: str | None = Query(default=None)):
        _check_http_token(token)
        return sanitize_for_json(get_stats(live_store, db))

    @app.get("/api/zones")
    def api_zones(token: str | None = Query(default=None)):
        _check_http_token(token)
        return sanitize_for_json(zones_payload(config))

    @app.get("/api/config")
    def api_config(token: str | None = Query(default=None)):
        _check_http_token(token)
        return sanitize_for_json(app_config_payload(config))

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        host_header = websocket.headers.get("host")
        if not _origin_allowed(origin, host_header):
            await websocket.close(code=1008)
            return
        token = websocket.query_params.get("token") or websocket.headers.get(
            "x-pyaerial-token"
        )
        if not _token_ok(config, token):
            await websocket.close(code=1008)
            return
        await broadcaster.connect(websocket)
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    req = json.loads(data)
                except Exception as parse_exc:
                    log.error("Error parsing WS message: %s", parse_exc)
                    await websocket.send_json(
                        {
                            "type": "response",
                            "id": None,
                            "success": False,
                            "error": "Invalid request",
                        }
                    )
                    continue
                if not (isinstance(req, dict) and req.get("type") == "request"):
                    continue
                req_id = req.get("id")
                action = req.get("action")
                params = req.get("params") or {}
                try:
                    res_data = await asyncio.to_thread(
                        ws_request_handler, action, params
                    )
                    await websocket.send_json(
                        {
                            "type": "response",
                            "id": req_id,
                            "success": True,
                            "data": sanitize_for_json(res_data),
                        }
                    )
                except Exception as inner_exc:
                    log.error("Error executing action %s: %s", action, inner_exc)
                    await websocket.send_json(
                        {
                            "type": "response",
                            "id": req_id,
                            "success": False,
                            "error": "Request failed",
                        }
                    )
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
            raise HTTPException(503, _FRONTEND_HINT)
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path.startswith("ws/"):
            raise HTTPException(404)
        if ".." in Path(full_path).parts:
            raise HTTPException(404)
        file_path = _safe_static_path(full_path)
        if file_path is None:
            if full_path and "." in full_path.rsplit("/", 1)[-1]:
                raise HTTPException(404)
        elif file_path.is_file():
            return FileResponse(file_path)
        index = _STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(
            503,
            _FRONTEND_HINT,
        )

    return app


def run_webapp(
    config_path: str = "config.yaml",
    *,
    aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
    host: str = "127.0.0.1",
    port: int = 10090,
    mock: bool = False,
) -> None:
    try:
        aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None
    except Exception as e:
        log.warning("Could not initialize AircraftDB at %s: %s", aircraft_db_path, e)
        aircraft_db = None
    client = None
    live_store = None
    engine = None

    if mock:
        log.info(
            "Running in MOCK mode with simulated ADS-B feeder feeding real tracking & alerting engine."
        )
        try:
            config = load_config(config_path)
        except Exception:
            log.warning(
                "Could not load configuration %s; falling back to defaults",
                config_path,
                exc_info=True,
            )
            from pyaerial.config.schema import Config, HomeConfig, TrackingConfig

            config = Config(
                home=HomeConfig(latitude=35.7275, longitude=-78.6959),
                tracking=TrackingConfig(),
                receivers={},
            )

        from pyaerial.config.schema import ReceiverConfig
        from pyaerial.engine import Engine
        import threading

        config.receivers = {"mock": ReceiverConfig(type="mock")}
        # Isolated: in-memory live store only. Never touch the configured
        # Redis/Mongo URIs (those may be a real production stack).
        engine = Engine(config, aircraft_db_path=aircraft_db_path, isolated=True)
        engine_thread = threading.Thread(
            target=engine.run, daemon=True, name="mock-engine"
        )
        engine_thread.start()

        db_ref = (
            engine.mongo_store.db
            if engine.mongo_store and hasattr(engine.mongo_store, "db")
            else None
        )
        app = create_app(
            config=config,
            db=db_ref,
            live_store=engine.live_store,
            aircraft_db=aircraft_db,
        )
    else:
        config, client, db, live_store = connect_stores(config_path)
        app = create_app(
            config=config, db=db, live_store=live_store, aircraft_db=aircraft_db
        )

    index = _STATIC_DIR / "index.html"
    if not index.is_file():
        log.warning("%s", _FRONTEND_HINT)
    print(
        f"Starting PyAerial web portal {'[MOCK FEEDER MODE] ' if mock else ''}"
        f"on http://{host}:{port}"
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        print("\nStopping web server...")
    finally:
        if engine:
            engine.shutdown()
        if client:
            client.close()
        if live_store:
            live_store.close()
        if aircraft_db:
            aircraft_db.close()
