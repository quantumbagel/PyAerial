from __future__ import annotations

from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum
from pyaerial.store.history import HistoryStore
from helpers import make_config, make_rule
from pyaerial.config.schema import ZoneConfig


def _plane(*, icao: str = "abc123", callsign: str = "SWA123"):
    return {
        STORE_INFO: {STORE_ICAO: icao, STORE_CALLSIGN: callsign, "model": "A320"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.725, 1.0), Datum(35.726, 10.0)],
            STORE_LONG: [Datum(-78.695, 1.0), Datum(-78.694, 10.0)],
        },
        STORE_CALC_DATA: {
            STORE_HEADING: [Datum(45.0, 1.0)],
            STORE_HORIZ_SPEED: [Datum(200.0, 1.0)],
        },
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 1.0,
            STORE_MOST_RECENT_PACKET: 10.0,
        },
    }


def _store(tmp_path, **kwargs) -> HistoryStore:
    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[make_rule(name="warn", retain=True, dwell_seconds=1)],
            )
        }
    )
    return HistoryStore(
        tmp_path / "pyaerial.db",
        config=config,
        polygons={},
        **kwargs,
    )


def test_finalize_writes_flight_telemetry_and_alerts(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "icao": "abc123",
            "callsign": "SWA123",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
            "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
            "altitude": 300.0,
        }
    ]
    assert store.finalize_plane(_plane(), alerts=alerts) is True
    flights = store.list_flights()
    assert len(flights) == 1
    assert flights[0]["icao"] == "abc123"
    assert flights[0]["callsign"] == "SWA123"
    points = store.get_telemetry("abc123-1")
    assert len(points) == 2
    assert points[0]["latitude"] == 35.725
    assert points[0]["longitude"] == -78.695
    stored_alerts = store.get_alerts()
    assert len(stored_alerts) == 1
    assert stored_alerts[0]["rule"] == "warn"
    assert stored_alerts[0]["latitude"] == 35.725
    assert stored_alerts[0]["longitude"] == -78.695
    store.close()


def test_finalize_does_not_replace_completed_telemetry(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 80.0,
        }
    ]
    plane = _plane()
    assert store.finalize_plane(plane, alerts=alerts) is True
    first = store.get_telemetry("abc123-1")
    assert len(first) == 2
    shorter = _plane()
    shorter[STORE_RECV_DATA][STORE_LAT] = [Datum(35.726, 10.0)]
    shorter[STORE_RECV_DATA][STORE_LONG] = [Datum(-78.694, 10.0)]
    assert store.finalize_plane(shorter, alerts=alerts) is True
    second = store.get_telemetry("abc123-1")
    assert len(second) == 2
    assert second[0]["latitude"] == 35.725
    store.close()


def test_finalize_persists_alert_reason_dict_and_finite_eta(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "icao": "abc123",
            "callsign": "SWA123",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
            "eta": float("inf"),
            "reason": {"zones": {"pad": float("inf")}, "rule": "warn", "hook": "deactivate"},
            "position": {"type": "Point", "coordinates": [-78.695, 35.725]},
        }
    ]
    assert store.finalize_plane(_plane(), alerts=alerts) is True
    stored = store.get_alerts()
    assert len(stored) == 1
    assert stored[0]["eta"] is None
    assert stored[0]["reason"]["rule"] == "warn"
    assert stored[0]["reason"]["zones"]["pad"] is None
    store.close()


def test_unretained_flight_is_not_written(tmp_path):
    store = _store(tmp_path)
    assert store.finalize_plane(_plane(), alerts=[]) is True
    assert store.list_flights() == []
    store.close()


def test_search_and_range_filters(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
        }
    ]
    store.finalize_plane(_plane(), alerts=alerts)
    store.finalize_plane(_plane(icao="def456", callsign="DAL99"), alerts=alerts)
    found = store.list_flights(q="swa")
    assert [doc["icao"] for doc in found] == ["abc123"]
    starred = store.list_flights(q="SWA*")
    assert starred == []
    ranged = store.list_flights(since=5.0, until=15.0)
    assert len(ranged) == 2
    empty = store.list_flights(until=0.5)
    assert empty == []
    store.close()


def test_portal_history_queries_use_sqlite(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "alert_id": "abc123-1:pad:warn",
            "zone": "pad",
            "rule": "warn",
            "icao": "abc123",
            "callsign": "SWA123",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
            "latitude": 35.725,
            "longitude": -78.695,
        }
    ]
    store.finalize_plane(_plane(), alerts=alerts)
    summaries = get_history_flights(store, None)
    assert len(summaries) == 1
    assert summaries[0]["flight_id"] == "abc123-1"
    assert summaries[0]["alert_stats"]["episode_count"] == 1
    detail = get_flight_detail(
        "abc123-1", "history", live_store=None, history=store, aircraft_db=None
    )
    assert detail is not None
    assert detail["model"] == "A320"
    tel = get_telemetry(
        "abc123-1", "history", 0.0, live_store=None, history=store
    )
    assert len(tel) == 2
    hist_alerts = get_alerts("history", live_store=None, history=store, q="warn")
    assert len(hist_alerts) == 1
    stats = get_stats(None, store)
    assert stats["history"] is True
    assert stats["retained_flights"] == 1
    assert stats["historical_alerts"] == 1
    store.close()


def test_reset_and_delete_icao(tmp_path):
    store = _store(tmp_path)
    alerts = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 9.0,
        }
    ]
    store.finalize_plane(_plane(), alerts=alerts)
    store.finalize_plane(_plane(icao="def456", callsign="DAL99"), alerts=alerts)
    store.delete_icao("abc123")
    assert store.has_icao("abc123") is False
    assert store.has_icao("def456") is True
    store.reset_all()
    assert store.list_flights() == []
    assert store.count_alerts() == 0
    store.close()
