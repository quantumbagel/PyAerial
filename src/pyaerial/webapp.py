"""FastAPI portal process hosting WebSocket feeds and browser assets."""

from __future__ import annotations

import logging

import uvicorn

from pyaerial.api.app import create_app
from pyaerial.api.connect import connect_stores
from pyaerial.api.static import FRONTEND_HINT, STATIC_DIR
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.history import HistoryStore
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.webapp")

__all__ = ["create_app", "run_webapp", "WS_MAX_MESSAGE_BYTES"]

#: Largest inbound WebSocket frame uvicorn accepts (client requests are tiny).
WS_MAX_MESSAGE_BYTES = 64 * 1024
#: Inbound messages uvicorn buffers per connection before applying back-pressure.
WS_MAX_QUEUE = 16


def _open_stores(
    config_path: str, config: Config | None
) -> tuple[Config, HistoryStore, RedisLiveStore]:
    if config is None:
        return connect_stores(config_path)
    # Caller already loaded (and validated) the config; don't parse it twice.
    history = HistoryStore(config.database.path)
    live_store = RedisLiveStore(config.database.redis_uri)
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
    print(f"Starting PyAerial web portal on http://{host}:{port}")
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            ws_max_size=WS_MAX_MESSAGE_BYTES,
            ws_max_queue=WS_MAX_QUEUE,
            # Per-IP WebSocket caps need the real client address behind a proxy.
            proxy_headers=True,
            forwarded_allow_ips=",".join(config.web.trusted_proxies) or "127.0.0.1",
        )
    except KeyboardInterrupt:
        print("\nStopping web server...")
    finally:
        history.close()
        live_store.close()
        if aircraft_db:
            aircraft_db.close()
