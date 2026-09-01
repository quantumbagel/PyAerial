"""
Process entry for the FastAPI web portal.

Application factory and routes live in :mod:`pyaerial.api.app`.
"""

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
        from pyaerial.engine import isolated_config, start_isolated_engine

        config = isolated_config(config_path)
        engine = start_isolated_engine(config, aircraft_db_path=aircraft_db_path)
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

    index = STATIC_DIR / "index.html"
    if not index.is_file():
        log.warning("%s", FRONTEND_HINT)
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
