"""FastAPI application factory for the live flight portal."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pyaerial.api.beast import BeastHub
from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.payloads import sanitize_for_json
from pyaerial.api.spec import WS_STREAMS, websocket_api_spec
from pyaerial.api.static import mount_spa
from pyaerial.api.ws import handle_ws_request
from pyaerial.config.schema import Config
from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.history import HistoryStore
from pyaerial.store.live import LiveStore

log = logging.getLogger("pyaerial.webapp")

_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


def _origin_allowed(
    origin: str | None,
    allowed: list[str] | None = None,
) -> bool:
    if not origin:
        return True
    allowed = allowed or []
    if "*" in allowed:
        return True
    if _LOCAL_ORIGIN.match(origin):
        return True
    parsed = urlparse(origin)
    origin_host = (parsed.hostname or "").lower()
    origin_norm = origin.rstrip("/")
    allowed_norm = {item.rstrip("/") for item in allowed}
    if origin_norm in allowed_norm or origin_host in {item.lower() for item in allowed}:
        return True
    return False



def _cors_kwargs(config: Config) -> dict[str, Any]:
    origins = list(config.web.origins or [])
    local_regex = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
    if "*" in origins:
        return {
            "allow_origins": ["*"],
            "allow_credentials": False,
            "allow_methods": ["GET", "HEAD", "OPTIONS"],
            "allow_headers": ["*"],
        }
    return {
        "allow_origins": origins,
        "allow_origin_regex": local_regex,
        "allow_credentials": True,
        "allow_methods": ["GET", "HEAD", "OPTIONS"],
        "allow_headers": ["*"],
    }


_KNOWN_STREAMS = frozenset(WS_STREAMS)


def _requested_streams(websocket: WebSocket) -> list[str] | None:
    value = websocket.query_params.get("streams")
    if not value:
        return None
    names = [part.strip() for part in value.split(",") if part.strip()]
    chosen = [name for name in names if name in _KNOWN_STREAMS]
    return chosen or None


def create_app(
    *,
    config: Config,
    history: HistoryStore | None = None,
    live_store: LiveStore | None = None,
    aircraft_db: AircraftDB | None = None,
) -> FastAPI:
    broadcaster = LiveBroadcaster(
        live_store,
        aircraft_db,
        history=history,
    )
    beast_hub = BeastHub(
        live_store,
        host=config.web.beast_host,
        port=config.web.beast_port,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await broadcaster.start()
        await beast_hub.start()
        yield
        await beast_hub.stop()
        await broadcaster.stop()

    app = FastAPI(title="PyAerial Web Portal", lifespan=lifespan)
    app.state.history = history
    app.state.live_store = live_store
    app.state.beast_hub = beast_hub
    app.add_middleware(CORSMiddleware, **_cors_kwargs(config))

    def ws_request_handler(action: str, params: dict[str, Any]) -> Any:
        return handle_ws_request(
            action,
            params,
            config=config,
            history=history,
            live_store=live_store,
            aircraft_db=aircraft_db,
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        redis_ok = True
        history_ok = True
        engine_ok = True
        if live_store is not None:
            redis_ok = bool(live_store.ping())
            live_fn = getattr(live_store, "engine_is_live", None)
            if callable(live_fn):
                try:
                    engine_ok = bool(live_fn())
                except Exception:
                    engine_ok = False
            else:
                getter = getattr(live_store, "engine_seen_at", None)
                seen = getter() if callable(getter) else None
                engine_ok = (
                    isinstance(seen, (int, float))
                    and (time.time() - seen) < LIVE_ENGINE_TTL_SECONDS
                )
        if history is not None:
            history_ok = bool(history.ping())
        status = "ok" if redis_ok and history_ok and engine_ok else "degraded"
        code = 200 if redis_ok else 503
        return JSONResponse(
            {
                "status": status,
                "redis": redis_ok,
                "history": history_ok,
                "engine": engine_ok,
            },
            status_code=code,
        )

    @app.get("/api")
    def api_index():
        return websocket_api_spec(
            beast_host=config.web.beast_host,
            beast_port=beast_hub.port,
        )

    async def _reject(websocket: WebSocket, reason: str) -> None:
        await websocket.accept()
        await websocket.close(code=1008, reason=reason)

    async def _ws_handler(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if not _origin_allowed(origin, config.web.origins):
            await _reject(websocket, "origin not allowed")
            return
        await broadcaster.connect(
            websocket,
            streams=_requested_streams(websocket),
        )
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    req = json.loads(data)
                except Exception as parse_exc:
                    log.error("Error parsing WS message: %s", parse_exc)
                    broadcaster.send(
                        websocket,
                        {
                            "type": "response",
                            "id": None,
                            "success": False,
                            "error": "Invalid request",
                        },
                    )
                    continue
                if not (isinstance(req, dict) and req.get("type") == "request"):
                    continue
                req_id = req.get("id")
                action = req.get("action")
                params = req.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                if action == "subscribe":
                    requested = params.get("streams")
                    selected = broadcaster.set_streams(websocket, requested)
                    if requested and not selected:
                        broadcaster.send(
                            websocket,
                            {
                                "type": "response",
                                "id": req_id,
                                "success": False,
                                "error": "Unknown streams",
                            },
                        )
                        continue
                    broadcaster.send(
                        websocket,
                        {
                            "type": "response",
                            "id": req_id,
                            "success": True,
                            "data": {"streams": selected},
                        },
                    )
                    continue
                try:
                    res_data = await asyncio.to_thread(
                        ws_request_handler, action, params
                    )
                    if action == "fetchFlight" and res_data is None:
                        broadcaster.send(
                            websocket,
                            {
                                "type": "response",
                                "id": req_id,
                                "success": False,
                                "error": "not found",
                            },
                        )
                        continue
                    broadcaster.send(
                        websocket,
                        {
                            "type": "response",
                            "id": req_id,
                            "success": True,
                            "data": sanitize_for_json(res_data),
                        },
                    )
                except Exception as inner_exc:
                    log.error("Error executing action %s: %s", action, inner_exc)
                    broadcaster.send(
                        websocket,
                        {
                            "type": "response",
                            "id": req_id,
                            "success": False,
                            "error": "Request failed",
                        },
                    )
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("WebSocket handler failed")
        finally:
            broadcaster.disconnect(websocket)
            try:
                await websocket.close()
            except Exception:
                pass

    async def ws_live(websocket: WebSocket):
        await _ws_handler(websocket)

    async def ws_beast(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if not _origin_allowed(origin, config.web.origins):
            await _reject(websocket, "origin not allowed")
            return
        await beast_hub.run_websocket(websocket)

    app.add_api_websocket_route("/ws/live", ws_live)
    app.add_api_websocket_route("/ws", ws_live)
    app.add_api_websocket_route("/ws/beast", ws_beast)

    mount_spa(app)
    return app
