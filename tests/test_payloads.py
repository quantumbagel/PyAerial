"""Outgoing payload normalisation (placeholders, photos, alert ids)."""

from __future__ import annotations

import asyncio

import pytest

from helpers import make_config
from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.payloads import (
    alert_id_for,
    antenna_payload,
    clean_text,
    enrich_flight_detail,
    enrich_flight_summary,
    enrich_from_aircraft_db,
    flight_summary,
    format_active_alerts,
    format_alert,
    https_url,
)
from pyaerial.config.schema import ReceiverConfig
from pyaerial.receivers.dump1090 import Dump1090Receiver
from pyaerial.webapp import create_app


class _DB:
    available = True

    def __init__(self, meta: dict):
        self.meta = meta

    def is_cached(self, _icao):
        return True

    def lookup_cached(self, _icao):
        return dict(self.meta)

    def lookup_cached_fast(self, _icao):
        return dict(self.meta)


def test_placeholders_become_null():
    for value in ("Unknown Model", "Unknown Owner", "UNKNOWN", "N/A", "None", "null", "", "  "):
        assert clean_text(value) is None
    assert clean_text(" N123AB ") == "N123AB"
    assert clean_text(None) is None


def test_placeholder_does_not_shadow_real_enrichment():
    db = _DB({"model": "Cessna 172", "owner": "Unknown Owner", "registration": "N1"})
    summary = enrich_flight_summary(
        {"flight_id": "f", "icao": "abc", "model": "Unknown Model", "owner": ""}, db
    )
    assert summary["model"] == "Cessna 172"
    assert summary["owner"] is None
    assert summary["registration"] == "N1"


def test_history_summary_includes_registration():
    doc = {"_id": "f", "icao": "abc", "info": {"registration": "N555"}}
    summary = flight_summary(doc, None, None)
    assert summary["registration"] == "N555"
    from_db = flight_summary({"_id": "f", "icao": "abc"}, None, _DB({"registration": "G-ABCD"}))
    assert from_db["registration"] == "G-ABCD"


def test_live_and_history_summaries_share_field_set():
    live = enrich_flight_summary({"flight_id": "f", "icao": "abc"}, None)
    history = flight_summary({"_id": "f", "icao": "abc"}, None, None)
    for name in ("callsign", "model", "owner", "country", "aircraft_type", "registration"):
        assert name in live
        assert name in history


def test_photo_urls_must_be_https():
    assert https_url("https://t.plnspttrs.net/1.jpg") == "https://t.plnspttrs.net/1.jpg"
    for bad in ("http://example.com/x.jpg", "javascript:alert(1)", "data:image/png;base64,AA", "//x/y", None, 5):
        assert https_url(bad) is None
    db = _DB(
        {
            "photo_url": "http://insecure.example/p.jpg",
            "photo_link": "javascript:alert(1)",
        }
    )
    detail = enrich_flight_detail({"flight_id": "f", "icao": "abc"}, "abc", db)
    assert detail["photo_url"] is None
    assert detail["photo_link"] is None
    ok = _DB({"photo_url": {"src": "https://cdn.example/p.jpg"}, "photo_link": "https://planespotters.net/p/1"})
    detail = enrich_flight_detail({"flight_id": "f", "icao": "abc"}, "abc", ok)
    assert detail["photo_url"] == "https://cdn.example/p.jpg"
    assert detail["photo_link"] == "https://planespotters.net/p/1"


def test_active_alert_ids_are_never_empty():
    alerts = format_active_alerts(
        {"_id": "f1", "active_alerts": [{"zone": "pad", "rule": "warn"}, {"alert_id": ""}]}
    )
    assert alerts[0]["alert_id"] == "f1:pad:warn"
    assert alerts[1]["alert_id"] == "f1::"
    live = enrich_flight_summary(
        {"flight_id": "f2", "icao": "abc", "active_alerts": [{"zone": "z", "rule": "r"}]},
        None,
    )
    assert live["active_alerts"][0]["alert_id"] == "f2:z:r"


def test_alert_id_fallback_is_shared():
    doc = {"flight_id": "f", "zone": "z", "rule": "r"}
    assert alert_id_for(doc) == "f:z:r"
    assert format_alert(doc)["alert_id"] == "f:z:r"
    assert alert_id_for({"alert_id": "keep"}) == "keep"
    assert format_alert({"callsign": "UNKNOWN"})["callsign"] is None


# --- enrichment.enabled: false -------------------------------------------------


class _RecordingDB:
    """AircraftDB stand-in; ``lookup_cached`` is the call that goes online."""

    available = True

    def __init__(self) -> None:
        self.online: list[str] = []
        self.local: list[str] = []

    def is_cached(self, icao: str) -> bool:
        return False

    def lookup_cached(self, icao: str):
        self.online.append(icao)
        return {"registration": "N1ONLINE"}

    def lookup_cached_fast(self, icao: str):
        self.local.append(icao)
        return {"registration": "N1LOCAL", "photo_url": None}

    def close(self) -> None:
        pass


class _OneFlight:
    def get_flights(self):
        return [{"flight_id": "f1", "icao": "abc123"}]


@pytest.mark.parametrize("enabled", [True, False])
def test_portal_lookups_follow_enrichment_enabled(enabled):
    config = make_config()
    config.enrichment.enabled = enabled
    db = _RecordingDB()
    app = create_app(config=config, aircraft_db=db)
    broadcaster: LiveBroadcaster = app.state.broadcaster
    broadcaster.live_store = _OneFlight()

    async def _scenario() -> None:
        broadcaster._clients = {object(): object()}  # type: ignore[dict-item]
        await broadcaster._background_tick()
        for _ in range(5):
            await asyncio.sleep(0.02)

    asyncio.run(_scenario())
    meta = enrich_from_aircraft_db("def456", broadcaster.aircraft_db)
    if enabled:
        assert db.online == ["abc123"]
    else:
        assert db.online == []  # nothing goes to hexdb.io / Planespotters
        assert meta["registration"] == "N1LOCAL"  # the local cache still works
        assert "def456" in db.local


# --- fetchConfig receivers and telemetry payloads ----------------------------


def _receiver_format(cfg: ReceiverConfig) -> str:
    receiver = Dump1090Receiver.__new__(Dump1090Receiver)
    receiver.configure(cfg.receiver_arguments())
    return receiver.format


def test_dump1090_format_matches_the_receiver():
    receivers = {
        "default": ReceiverConfig(type="dump1090"),
        "beast_port": ReceiverConfig(type="dump1090", port=30005),
        "avr_port": ReceiverConfig(type="dump1090", port=30002),
        "custom_port": ReceiverConfig(type="dump1090", port=31005),
        "binary": ReceiverConfig(type="dump1090", port=31005, format="binary"),
        "raw": ReceiverConfig(type="dump1090", port=31002, format="raw"),
    }
    payload = antenna_payload(make_config(receivers=receivers))
    formats = {item["name"]: item.get("format") for item in payload["receivers"]}
    assert formats == {name: _receiver_format(cfg) for name, cfg in receivers.items()}
    assert formats["default"] == "beast"
    assert formats["raw"] == "avr"


def test_readsb_reports_its_wire_format():
    receivers = {
        "readsb": ReceiverConfig(type="readsb"),
        "readsb_avr": ReceiverConfig(type="readsb", port=30002),
    }
    payload = antenna_payload(make_config(receivers=receivers))
    formats = {item["name"]: item.get("format") for item in payload["receivers"]}
    assert formats == {"readsb": "beast", "readsb_avr": "avr"}


def test_telemetry_point_passes_gnss_height_only_when_known():
    from pyaerial.api.payloads import telemetry_point

    base = {"timestamp": 1.0, "latitude": 1.0, "longitude": 2.0, "altitude": 100.0}
    assert telemetry_point({**base, "gnss_height": 10972.8})["gnss_height"] == 10972.8
    assert "gnss_height" not in telemetry_point({**base, "gnss_height": None})
    assert "gnss_height" not in telemetry_point(base)
    # Provenance fields keep passing None through (verdict not known yet).
    assert telemetry_point({**base, "integrity_score": None})["integrity_score"] is None


def test_unknown_integrity_with_null_score_flows_through_payloads():
    import json

    from pyaerial.api.broadcaster import _flights_sig
    from pyaerial.api.payloads import (
        enrich_flight_summary,
        flight_summary,
        sanitize_for_json,
    )

    unknown = {"score": None, "status": "unknown", "checks": {}}
    history = flight_summary(
        {"_id": "f1", "icao": "abc123", "info": {"integrity": unknown}}, None, None
    )
    live = enrich_flight_summary({"flight_id": "f2", "icao": "abc", "integrity": unknown}, None)
    for summary in (history, live):
        assert summary["integrity"] == unknown
        assert json.loads(json.dumps(sanitize_for_json(summary)))["integrity"]["score"] is None
    scored = {**live, "integrity": {**unknown, "score": 1.0, "status": "ok"}}
    assert _flights_sig([live]) != _flights_sig([scored])


def test_format_alert_passes_ended_by_through():
    import json

    from pyaerial.api.payloads import format_alert

    base = {"alert_id": "a", "flight_id": "f", "zone": "z", "rule": "r"}
    assert format_alert({**base, "reason": {"ended_by": "lost"}})["ended_by"] == "lost"
    # History rows may hold the reason as JSON text.
    as_text = json.dumps({"ended_by": "segment", "rule": "r"})
    assert format_alert({**base, "reason": as_text})["ended_by"] == "segment"
    for reason in (None, "plain text", {"ended_by": ""}, {"ended_by": 5}, "{broken", ["x"]):
        assert format_alert({**base, "reason": reason})["ended_by"] is None
    assert format_alert({**base, "ended_by": "restart"})["ended_by"] == "restart"


def test_fetch_config_does_not_expose_receiver_endpoints():
    from pyaerial.api.payloads import app_config_payload
    from pyaerial.config.schema import ReceiverPositionConfig

    receivers = {
        "roof": ReceiverConfig(
            type="dump1090",
            host="10.0.0.7",
            port=30005,
            position=ReceiverPositionConfig(latitude=35.7, longitude=-78.6),
            position_url="http://10.0.0.7:8080/data/receiver.json",
        )
    }
    payload = app_config_payload(make_config(receivers=receivers))
    (roof,) = payload["receivers"]
    # The portal draws receiver positions and shows names and wire formats.
    assert roof["name"] == "roof" and roof["format"] == "beast"
    assert roof["position"]["latitude"] == 35.7
    text = repr(payload)
    assert "10.0.0.7" not in text and "30005" not in text
