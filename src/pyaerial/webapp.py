"""FastAPI portal process hosting WebSocket feeds and browser assets."""

from __future__ import annotations

import logging

import uvicorn

from pyaerial.api.app import create_app
from pyaerial.api.connect import connect_stores
from pyaerial.api.static import FRONTEND_HINT, STATIC_DIR
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.enrich.aircraft_db import AircraftDB

log = logging.getLogger("pyaerial.webapp")

__all__ = ["create_app", "run_webapp"]


def run_webapp(
    config_path: str = "config.yaml",
    *,
    aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
    host: str = "127.0.0.1",
    port: int = 10090,
) -> None:
    try:
        aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None
    except Exception as e:
        log.warning("Could not initialize AircraftDB at %s: %s", aircraft_db_path, e)
        aircraft_db = None
    history = None
    live_store = None

    config, history, live_store = connect_stores(config_path)
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
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        print("\nStopping web server...")
    finally:
        if history:
            history.close()
        if live_store:
            live_store.close()
        if aircraft_db:
            aircraft_db.close()
