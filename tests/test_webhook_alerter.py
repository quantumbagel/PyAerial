from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from pyaerial.alerters.webhook import WebhookAlerter


def _alerter(url: str = "https://example.com/hook") -> WebhookAlerter:
    return WebhookAlerter({"url": url})


def test_configure_rejects_localhost_prefix_bypass():
    with pytest.raises(ValueError, match="https"):
        WebhookAlerter({"url": "http://localhost.attacker.example/hook"})
    with pytest.raises(ValueError, match="https"):
        WebhookAlerter({"url": "http://localhost@evil.example/hook"})


def test_configure_accepts_local_http_and_https():
    WebhookAlerter({"url": "http://127.0.0.1:8080/hook"})
    WebhookAlerter({"url": "https://discord.com/api/webhooks/x"})


def test_alert_retries_then_succeeds(monkeypatch):
    alerter = _alerter()
    attempts = {"n": 0}

    def _request(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.ConnectionError("down")
        resp = Mock()
        resp.raise_for_status = Mock()
        return resp

    monkeypatch.setattr("pyaerial.alerters.webhook.requests.request", _request)
    monkeypatch.setattr("pyaerial.alerters.webhook.time.sleep", lambda _delay: None)
    alerter.alert({"icao": "abc123", "type": "warn", "zone": "pad", "eta": 10}, {})
    assert attempts["n"] == 3


def test_alert_gives_up_after_retries(monkeypatch):
    alerter = _alerter()

    def _request(*args, **kwargs):
        raise requests.Timeout("nope")

    sleeps: list[float] = []
    monkeypatch.setattr("pyaerial.alerters.webhook.requests.request", _request)
    monkeypatch.setattr(
        "pyaerial.alerters.webhook.time.sleep", lambda delay: sleeps.append(delay)
    )
    alerter.alert({"icao": "abc123", "type": "warn", "zone": "pad", "eta": 10}, {})
    assert sleeps == [0.5, 1.0]
