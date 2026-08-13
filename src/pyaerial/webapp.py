"""
FastAPI web portal for live flight tracking.

Reads live flights from Redis and retained historical flights from MongoDB.
Serves a React SPA from pyaerial/static and pushes live updates over WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pymongo
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.connect import connect_stores
from pyaerial.api.payloads import sanitize_for_json
from pyaerial.api.ws import handle_ws_request
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.mock_store import MockStore
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.webapp")

_STATIC_DIR = Path(__file__).parent / "static"


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


def create_app(*, config: Config, db: pymongo.database.Database | None = None,
               live_store: RedisLiveStore | None = None, aircraft_db: AircraftDB | None = None,
               mock_store: MockStore | None = None) -> FastAPI:
    broadcaster = LiveBroadcaster(live_store, aircraft_db, mock_store=mock_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await broadcaster.start()
        yield
        await broadcaster.stop()

    app = FastAPI(title="PyAerial Web Portal", lifespan=lifespan)
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
            mock_store=mock_store,
        )

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
                            res_data = await asyncio.to_thread(ws_request_handler, action, params)
                            await websocket.send_json({
                                "type": "response",
                                "id": req_id,
                                "success": True,
                                "data": sanitize_for_json(res_data)
                            })
                        except Exception as inner_exc:
                            log.error("Error executing action %s: %s", action, inner_exc)
                            await websocket.send_json({
                                "type": "response",
                                "id": req_id,
                                "success": False,
                                "error": str(inner_exc)
                            })
                except Exception as parse_exc:
                    log.error("Error parsing WS message: %s", parse_exc)
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
            "Frontend not built. Run: cd web && npm install && npm run build",
        )

    return app


def run_webapp(config_path: str = "config.yaml", *,
               aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
               host: str = "0.0.0.0", port: int = 10090,
               mock: bool = False,
               mock_delay: float = 0.5) -> None:
    try:
        aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None
    except Exception as e:
        log.warning("Could not initialize AircraftDB at %s: %s", aircraft_db_path, e)
        aircraft_db = None
    client = None
    live_store = None
    engine = None

    if mock:
        log.info("Running in MOCK mode with simulated ADS-B feeder feeding real tracking & alerting engine.")
        try:
            config = load_config(config_path)
        except Exception:
            log.warning(
                "Could not load configuration %s; falling back to defaults", config_path,
                exc_info=True,
            )
            from pyaerial.config.schema import Config, HomeConfig, TrackingConfig
            config = Config(home=HomeConfig(latitude=35.7275, longitude=-78.6959), tracking=TrackingConfig(), receivers={})

        from pyaerial.config.schema import ReceiverConfig
        from pyaerial.engine import Engine
        import threading

        config.receivers = {"mock": ReceiverConfig(type="mock")}
        engine = Engine(config, aircraft_db_path=aircraft_db_path)
        engine_thread = threading.Thread(target=engine.run, daemon=True, name="mock-engine")
        engine_thread.start()

        db_ref = engine.mongo_store.db if engine.mongo_store and hasattr(engine.mongo_store, "db") else None
        app = create_app(config=config, db=db_ref, live_store=engine.live_store, aircraft_db=aircraft_db)
    else:
        config, client, db, live_store = connect_stores(config_path)
        app = create_app(config=config, db=db, live_store=live_store, aircraft_db=aircraft_db)

    print(f"Starting PyAerial web portal {'[MOCK FEEDER MODE] ' if mock else ''}on http://localhost:{port}")
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
