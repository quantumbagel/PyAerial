"""FastAPI application factory for the live flight portal."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.payloads import antenna_payload, sanitize_for_json
from pyaerial.api.spec import WS_STREAMS, websocket_api_spec
from pyaerial.api.static import mount_spa
from pyaerial.api.ws import handle_ws_request
from pyaerial.config.schema import Config
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.history import HistoryStore
from pyaerial.store.live import LiveStore

log = logging.getLogger("pyaerial.webapp")

_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


def _origin_allowed(
    origin: str | None,
    host_header: str | None,
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
    if host_header:
        origin_netloc = (parsed.netloc or "").lower()
        host_norm = host_header.split(",")[0].strip().lower()
        if origin_netloc and origin_netloc == host_norm:
            return True
    return False


def _token_ok(config: Config, token: str | None) -> bool:
    expected = config.web.token
    if not expected:
        return True
    if not token:
        return False
    return hmac.compare_digest(str(token), str(expected))


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
        antenna=antenna_payload(config),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await broadcaster.start()
        yield
        await broadcaster.stop()

    app = FastAPI(title="PyAerial Web Portal", lifespan=lifespan)
    app.state.history = history
    app.state.live_store = live_store
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
        if live_store is not None:
            redis_ok = bool(live_store.ping())
        if history is not None:
            history_ok = bool(history.ping())
        status = "ok" if redis_ok and history_ok else "degraded"
        code = 200 if redis_ok else 503
        return JSONResponse(
            {"status": status, "redis": redis_ok, "history": history_ok},
            status_code=code,
        )

    @app.get("/api")
    def api_index():
        return websocket_api_spec()

    async def _reject(websocket: WebSocket, reason: str) -> None:
        await websocket.accept()
        await websocket.close(code=1008, reason=reason)

    async def _ws_handler(websocket: WebSocket, *, raw_only: bool) -> None:
        origin = websocket.headers.get("origin")
        host_header = websocket.headers.get("host")
        if not _origin_allowed(origin, host_header, config.web.origins):
            await _reject(websocket, "origin not allowed")
            return
        token = websocket.query_params.get("token")
        if not _token_ok(config, token):
            await _reject(websocket, "unauthorized")
            return
        await broadcaster.connect(
            websocket,
            streams=None if raw_only else _requested_streams(websocket),
            raw_only=raw_only,
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
                if raw_only:
                    broadcaster.send(
                        websocket,
                        {
                            "type": "response",
                            "id": req_id,
                            "success": False,
                            "error": "Live actions are not available on /ws/raw",
                        },
                    )
                    continue
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
        await _ws_handler(websocket, raw_only=False)

    async def ws_raw(websocket: WebSocket):
        await _ws_handler(websocket, raw_only=True)

    app.add_api_websocket_route("/ws/live", ws_live)
    app.add_api_websocket_route("/ws", ws_live)
    app.add_api_websocket_route("/ws/raw", ws_raw)

    mount_spa(app)
    return app
