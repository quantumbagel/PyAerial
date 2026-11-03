"""FastAPI application factory for the live flight portal."""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketState

from pyaerial.api.beast import BeastHub
from pyaerial.api.broadcaster import _CLIENT_QUEUE_BYTES as CLIENT_QUEUE_BYTES
from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.api.payloads import OfflineAircraftDB
from pyaerial.api.metrics import WebSocketStats, add_metrics_route
from pyaerial.api.queries import engine_is_live
from pyaerial.api.security import (
    ConnectionLimiter,
    BucketRegistry,
    SecurityHeadersMiddleware,
    TokenBucket,
    client_ip,
    localhost_origin_regex,
    origin_allowed,
)
from pyaerial.api.spec import WS_STREAMS, websocket_api_spec
from pyaerial.api.static import mount_spa
from pyaerial.api.ws import BadRequest, handle_ws_request
from pyaerial.config.schema import Config
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.history import HistoryStore, HistoryUnavailable
from pyaerial.store.live import LiveStore
from pyaerial.store.redis_live import LiveUnavailable

log = logging.getLogger("pyaerial.webapp")

#: Request ids are echoed back verbatim; keep them small and scalar.
_MAX_REQUEST_ID_LEN = 128
#: Longest a request is delayed by the rate limiter before being refused.
_MAX_THROTTLE_SECONDS = 1.0


def _cors_kwargs(config: Config) -> dict[str, Any]:
    origins = list(config.web.origins or [])
    if "*" in origins:
        return {
            "allow_origins": ["*"],
            "allow_credentials": False,
            "allow_methods": ["GET", "HEAD", "OPTIONS"],
            "allow_headers": ["*"],
        }
    kwargs: dict[str, Any] = {
        "allow_origins": origins,
        "allow_credentials": False,
        "allow_methods": ["GET", "HEAD", "OPTIONS"],
        "allow_headers": ["*"],
    }
    if config.web.allow_localhost_origins:
        kwargs["allow_origin_regex"] = localhost_origin_regex()
    return kwargs


_KNOWN_STREAMS = frozenset(WS_STREAMS)


def _requested_streams(websocket: WebSocket) -> list[str] | None:
    value = websocket.query_params.get("streams")
    if not value:
        return None
    names = [part.strip() for part in value.split(",") if part.strip()]
    chosen = [name for name in names if name in _KNOWN_STREAMS]
    return chosen or None


def _request_id(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) <= _MAX_REQUEST_ID_LEN:
        return value
    return None


def _error(req_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "type": "response",
        "id": req_id,
        "success": False,
        "error": message,
        "code": code,
    }


def create_app(
    *,
    config: Config,
    history: HistoryStore | None = None,
    live_store: LiveStore | None = None,
    aircraft_db: AircraftDB | None = None,
) -> FastAPI:
    web = config.web
    if aircraft_db is not None and not config.enrichment.enabled:
        # Every portal lookup then stays in the local cache: nothing is sent
        # to hexdb.io or Planespotters.
        aircraft_db = OfflineAircraftDB(aircraft_db)  # type: ignore[assignment]
    broadcaster = LiveBroadcaster(
        live_store,
        aircraft_db,
        history=history,
    )
    beast_hub = BeastHub(
        live_store,
        host=web.beast_host,
        port=web.beast_port,
        max_clients=web.beast_max_clients,
        max_clients_per_ip=web.beast_max_clients_per_ip,
    )
    # Request handlers get their own bounded pool so a burst of history
    # queries cannot starve the broadcaster's asyncio.to_thread() work.
    request_pool = ThreadPoolExecutor(
        max_workers=web.ws_request_workers, thread_name_prefix="pyaerial-ws"
    )
    limiter = ConnectionLimiter(web.ws_max_connections, web.ws_max_connections_per_ip)
    # One request budget per client IP, shared by all of its connections (a
    # client could otherwise multiply the per-connection rate by opening more).
    # Kept after disconnect until refilled, so reconnecting resets nothing.
    ip_buckets = BucketRegistry(web.ws_rate_limit_per_ip, web.ws_rate_burst_per_ip)
    websockets = WebSocketStats()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await broadcaster.start()
        try:
            await beast_hub.start()
            try:
                config.web.beast_port = beast_hub.port
                yield
            finally:
                await beast_hub.stop()
        finally:
            await broadcaster.stop()
            request_pool.shutdown(wait=False, cancel_futures=True)

    docs = web.enable_docs
    app = FastAPI(
        title="PyAerial Web Portal",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.history = history
    app.state.live_store = live_store
    app.state.beast_hub = beast_hub
    app.state.broadcaster = broadcaster
    app.state.ws_limiter = limiter
    app.add_middleware(CORSMiddleware, **_cors_kwargs(config))
    app.add_middleware(SecurityHeadersMiddleware)
    if web.allowed_hosts:
        # Loopback stays reachable so the container healthcheck, local access
        # and the Vite dev proxy keep working once a hostname list is set.
        hosts = list(dict.fromkeys([*web.allowed_hosts, "localhost", "127.0.0.1"]))
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    def ws_request_handler(req_id: Any, action: str, params: dict[str, Any]) -> str:
        """Run one RPC and return the serialized response message."""
        result = handle_ws_request(
            action,
            params,
            config=config,
            history=history,
            live_store=live_store,
            aircraft_db=aircraft_db,
        )
        if action == "fetchFlight" and result is None:
            return json_dumps(_error(req_id, "not_found", "not found"))
        response: dict[str, Any] = {
            "type": "response",
            "id": req_id,
            "success": True,
            "data": result,
        }
        if action == "fetchTelemetry":
            # Beside ``data`` so it stays a plain array for existing clients.
            response["truncated"] = bool(getattr(result, "truncated", False))
        # Sanitising and serializing large telemetry lists is CPU work; keep
        # it off the event loop. The text is queued as-is (counted in bytes).
        return json_dumps(response)

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
            engine_ok = engine_is_live(live_store)
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
            beast_host=web.beast_host,
            beast_port=beast_hub.port,
            limits={
                "ws_max_connections": web.ws_max_connections,
                "ws_max_connections_per_ip": web.ws_max_connections_per_ip,
                "ws_max_inflight": web.ws_max_inflight,
                "ws_rate_limit": web.ws_rate_limit,
                "ws_rate_burst": web.ws_rate_burst,
                "ws_rate_limit_per_ip": web.ws_rate_limit_per_ip,
                "ws_rate_burst_per_ip": web.ws_rate_burst_per_ip,
                "ws_max_message_bytes": 64 * 1024,
                "ws_client_queue_bytes": CLIENT_QUEUE_BYTES,
                "beast_max_clients": web.beast_max_clients,
                "beast_max_clients_per_ip": web.beast_max_clients_per_ip,
            },
        )

    async def _reject(websocket: WebSocket, code: int, reason: str) -> None:
        await websocket.accept()
        await websocket.close(code=code, reason=reason)

    async def _admit(websocket: WebSocket) -> str | None:
        """Origin + connection-cap check. Returns the client key, or None."""
        origin = websocket.headers.get("origin")
        if not origin_allowed(
            origin,
            web.origins,
            host_header=websocket.headers.get("host"),
            allow_localhost=web.allow_localhost_origins,
            allowed_hosts=web.allowed_hosts,
        ):
            log.info("WebSocket from origin %r rejected", origin)
            await _reject(websocket, 1008, "origin not allowed")
            return None
        ip = client_ip(websocket)
        reason = limiter.acquire(ip)
        if reason is not None:
            log.warning("WebSocket from %s rejected: %s", ip, reason)
            await _reject(websocket, 1013, reason)
            return None
        return ip

    async def _run_request(
        websocket: WebSocket,
        req_id: Any,
        action: Any,
        params: dict[str, Any],
        slots: asyncio.Semaphore,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            try:
                text = await loop.run_in_executor(
                    request_pool, ws_request_handler, req_id, action, params
                )
            except BadRequest as exc:
                log.debug("Bad WS request %r: %s", action, exc)
                text = json_dumps(_error(req_id, "bad_request", str(exc)))
            except HistoryUnavailable:
                log.warning(
                    "WS action %r: history archive unavailable", action, exc_info=True
                )
                text = json_dumps(
                    _error(req_id, "history_unavailable", "History archive unavailable")
                )
            except LiveUnavailable:
                log.warning("WS action %r: live store unavailable", action, exc_info=True)
                text = json_dumps(
                    _error(req_id, "live_unavailable", "Live store unavailable")
                )
            except (ValueError, TypeError):
                log.warning("WS action %r rejected its parameters", action, exc_info=True)
                text = json_dumps(
                    _error(req_id, "bad_request", "Invalid request parameters")
                )
            except Exception:
                log.exception("Error executing WS action %r", action)
                text = json_dumps(_error(req_id, "internal_error", "Request failed"))
            # Hold the in-flight slot until the writer has actually sent the
            # response: a client that stops reading then stops being served,
            # instead of piling finished responses up in server memory.
            sent = broadcaster.send_response(websocket, text)
            if sent is not None:
                await sent
        finally:
            slots.release()

    def _handle_subscribe(websocket: WebSocket, req_id: Any, params: dict) -> None:
        requested = params.get("streams")
        selected = broadcaster.set_streams(websocket, requested)
        if requested and not selected:
            broadcaster.send(websocket, _error(req_id, "bad_request", "Unknown streams"))
            return
        broadcaster.send(
            websocket,
            {
                "type": "response",
                "id": req_id,
                "success": True,
                "data": {"streams": selected},
            },
        )

    async def _serve_live(websocket: WebSocket, ip_bucket: TokenBucket) -> None:
        slots = asyncio.Semaphore(web.ws_max_inflight)
        bucket = TokenBucket(web.ws_rate_limit, web.ws_rate_burst)
        tasks: set[asyncio.Task] = set()
        try:
            await broadcaster.connect(websocket, streams=_requested_streams(websocket))
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None:
                    broadcaster.send(
                        websocket, _error(None, "bad_request", "Expected a JSON text frame")
                    )
                    continue
                try:
                    req = json.loads(text)
                except ValueError as parse_exc:
                    log.debug("Ignoring malformed WS message: %s", parse_exc)
                    broadcaster.send(websocket, _error(None, "bad_request", "Invalid request"))
                    continue
                if not (isinstance(req, dict) and req.get("type") == "request"):
                    continue
                req_id = _request_id(req.get("id"))
                # Over the rate, requests are delayed (we stop reading the
                # socket meanwhile); only an unreasonably long wait is refused.
                delay = bucket.reserve(_MAX_THROTTLE_SECONDS)
                ip_delay = ip_bucket.reserve(_MAX_THROTTLE_SECONDS)
                if delay is None or ip_delay is None:
                    websockets.rate_limited += 1
                    broadcaster.send(
                        websocket, _error(req_id, "rate_limited", "Rate limit exceeded")
                    )
                    continue
                delay = max(delay, ip_delay)
                if delay > 0:
                    await asyncio.sleep(delay)
                action = req.get("action")
                params = req.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                if action == "subscribe":
                    _handle_subscribe(websocket, req_id, params)
                    continue
                # Back-pressure: stop reading until an in-flight slot frees.
                await slots.acquire()
                task = asyncio.create_task(
                    _run_request(websocket, req_id, action, params, slots)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("WebSocket handler failed")
        finally:
            for task in list(tasks):
                task.cancel()
            broadcaster.disconnect(websocket)
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.close()
                except (RuntimeError, OSError) as exc:
                    log.debug("WebSocket close failed: %s", type(exc).__name__)

    async def ws_live(websocket: WebSocket):
        ip = await _admit(websocket)
        if ip is None:
            return
        ip_bucket = ip_buckets.acquire(ip)
        try:
            await _serve_live(websocket, ip_bucket)
        finally:
            limiter.release(ip)
            ip_buckets.release(ip)

    async def ws_beast(websocket: WebSocket):
        ip = await _admit(websocket)
        if ip is None:
            return
        try:
            await beast_hub.run_websocket(websocket)
        finally:
            limiter.release(ip)

    async def ws_raw(websocket: WebSocket):
        ip = await _admit(websocket)
        if ip is None:
            return
        try:
            await beast_hub.run_json_websocket(websocket)
        finally:
            limiter.release(ip)

    app.add_api_websocket_route("/ws/live", websockets.track("/ws/live", ws_live))
    app.add_api_websocket_route("/ws", websockets.track("/ws/live", ws_live))
    app.add_api_websocket_route("/ws/beast", websockets.track("/ws/beast", ws_beast))
    app.add_api_websocket_route("/ws/raw", websockets.track("/ws/raw", ws_raw))
    add_metrics_route(
        app,
        config=config,
        history=history,
        live_store=live_store,
        broadcaster=broadcaster,
        beast_hub=beast_hub,
        websockets=websockets,
    )

    mount_spa(app)
    return app
