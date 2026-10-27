"""Outgoing payload normalisation (placeholders, photos, alert ids)."""

from __future__ import annotations

from pyaerial.api.payloads import (
    alert_id_for,
    clean_text,
    enrich_flight_detail,
    enrich_flight_summary,
    flight_summary,
    format_active_alerts,
    format_alert,
    https_url,
)


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
