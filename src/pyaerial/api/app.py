"""FastAPI application factory for the live flight portal."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import pymongo
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.payloads import sanitize_for_json
from pyaerial.api.static import mount_spa
from pyaerial.api.ws import handle_ws_request
from pyaerial.config.schema import Config
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.live import LiveStore

log = logging.getLogger("pyaerial.webapp")

_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


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


def create_app(
    *,
    config: Config,
    db: pymongo.database.Database | None = None,
    live_store: LiveStore | None = None,
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

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        redis_ok = True
        mongo_ok = True
        if live_store is not None:
            redis_ok = bool(live_store.ping())
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

    mount_spa(app)
    return app
