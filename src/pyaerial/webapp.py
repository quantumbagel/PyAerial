"""FastAPI portal process hosting WebSocket feeds and browser assets."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import uvicorn
from uvicorn.protocols.http.auto import AutoHTTPProtocol

from pyaerial.api.app import create_app
from pyaerial.api.connect import connect_stores, open_portal_stores
from pyaerial.api.static import FRONTEND_HINT, STATIC_DIR
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.archive import ArchiveReader
from pyaerial.store.live import LiveReader

log = logging.getLogger("pyaerial.webapp")

__all__ = ["create_app", "run_webapp", "uvicorn_options", "WS_MAX_MESSAGE_BYTES"]

#: Largest inbound WebSocket frame uvicorn accepts (client requests are tiny).
WS_MAX_MESSAGE_BYTES = 64 * 1024
#: Inbound messages uvicorn buffers per connection before applying back-pressure.
WS_MAX_QUEUE = 16
#: Seconds a connection may take to send a request's headers (slow-loris guard).
HEADER_TIMEOUT_SECONDS = 10.0
#: Idle keep-alive seconds after a response.
KEEP_ALIVE_SECONDS = 5


def _header_timeout_protocol(seconds: float) -> type[asyncio.Protocol]:
    """uvicorn's HTTP protocol, closing connections whose headers stall.

    ``timeout_keep_alive`` only covers the idle time after a response; a
    client that connects and sends nothing, or half a request line, would
    otherwise hold its socket forever. WebSocket upgrades hand the transport
    to another protocol and are left alone.
    """

    class HeaderTimeoutProtocol(AutoHTTPProtocol):  # type: ignore[misc, valid-type]
        _header_timer: asyncio.TimerHandle | None = None
        _armed_cycle: Any = None

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            super().connection_made(transport)  # type: ignore[arg-type]
            self._arm_header_timer()

        def _arm_header_timer(self) -> None:
            if self._header_timer is not None:
                self._header_timer.cancel()
            self._armed_cycle = self.cycle
            self._header_timer = self.loop.call_later(seconds, self._header_timeout)

        def _header_timeout(self) -> None:
            self._header_timer = None
            transport = self.transport
            if transport.is_closing() or transport.get_protocol() is not self:
                return  # closed, or now a WebSocket
            cycle = self.cycle
            if cycle is not self._armed_cycle:
                return  # a request's headers arrived; its cycle owns timing now
            if cycle is None or cycle.response_complete:
                transport.close()

        def on_response_complete(self) -> None:
            super().on_response_complete()
            if not self.transport.is_closing():
                self._arm_header_timer()  # the next request's headers

        def connection_lost(self, exc: Exception | None) -> None:
            if self._header_timer is not None:
                self._header_timer.cancel()
                self._header_timer = None
            super().connection_lost(exc)

    return HeaderTimeoutProtocol


def uvicorn_options(
    config: Config, *, header_timeout: float = HEADER_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """uvicorn settings for the portal (shared with the tests).

    The portal is still meant to sit behind a reverse proxy when exposed;
    these bound what one misbehaving client can hold on its own.
    """
    web = config.web
    return {
        "log_level": config.logging.level,
        # Use the process's logging setup (one format) instead of uvicorn's.
        "log_config": None,
        "http": _header_timeout_protocol(header_timeout),
        "ws_max_size": WS_MAX_MESSAGE_BYTES,
        "ws_max_queue": WS_MAX_QUEUE,
        "timeout_keep_alive": KEEP_ALIVE_SECONDS,
        # Open connections (HTTP + WebSocket) before new requests get 503.
        "limit_concurrency": max(1024, 2 * web.ws_max_connections + 64),
        "timeout_graceful_shutdown": 10,
        # Per-IP WebSocket caps need the real client address behind a proxy.
        "proxy_headers": True,
        "forwarded_allow_ips": ",".join(web.trusted_proxies) or "127.0.0.1",
    }


def _open_stores(
    config_path: str, config: Config | None
) -> tuple[Config, ArchiveReader, LiveReader]:
    if config is None:
        return connect_stores(config_path)
    # Caller already loaded (and validated) the config; don't parse it twice.
    # The portal opens the archive read-only; only the engine writes it.
    history, live_store = open_portal_stores(config)
    return config, history, live_store


def run_webapp(
    config_path: str = "config.yaml",
    *,
    config: Config | None = None,
    aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
    host: str = "127.0.0.1",
    port: int = 10090,
) -> None:
    config, history, live_store = _open_stores(config_path, config)
    try:
        aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None
    except Exception as e:
        log.warning("Could not initialize AircraftDB at %s: %s", aircraft_db_path, e)
        aircraft_db = None

    app = create_app(
        config=config,
        history=history,
        live_store=live_store,
        aircraft_db=aircraft_db,
    )

    index = STATIC_DIR / "index.html"
    if not index.is_file():
        log.warning("%s", FRONTEND_HINT)
    log.info("Starting PyAerial web portal on http://%s:%s", host, port)
    try:
        uvicorn.run(app, host=host, port=port, **uvicorn_options(config))
    except KeyboardInterrupt:
        log.info("Stopping web server")
    finally:
        history.close()
        live_store.close()
        if aircraft_db:
            aircraft_db.close()
