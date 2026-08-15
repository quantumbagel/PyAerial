from __future__ import annotations

from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_live_flights,
    get_telemetry,
)
from pyaerial.webapp import create_app
from helpers import make_config


def test_history_queries_tolerate_missing_db():
    assert get_history_flights(None, None) == []
    assert get_flight_detail("x", "history", live_store=None, db=None, aircraft_db=None) is None
    assert get_telemetry("x", "history", 0.0, live_store=None, db=None) == []
    assert get_alerts("history", live_store=None, db=None) == []


def test_live_queries_tolerate_missing_store():
    assert get_live_flights(None, None) == []
    assert get_flight_detail("x", "live", live_store=None, db=None, aircraft_db=None) is None
    assert get_telemetry("x", "live", 0.0, live_store=None, db=None) == []
    assert get_alerts("live", live_store=None, db=None) == []


def test_create_app_without_frontend_serves_503():
    app = create_app(config=make_config(), db=None, live_store=None, aircraft_db=None)
    assert app.title == "PyAerial Web Portal"
