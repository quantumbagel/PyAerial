from __future__ import annotations

import pytest

from pyaerial.config.loader import (
    ConfigError,
    _validate_cross_references,
    load_config,
    webhook_url_allowed,
)
from pyaerial.config.schema import AlertActionConfig, ReceiverConfig
from helpers import make_config, make_rule
from pyaerial.config.schema import ZoneConfig


def test_unknown_receiver_rejected():
    config = make_config(receivers={"x": ReceiverConfig(type="not-a-receiver")})
    with pytest.raises(ConfigError, match="unknown type"):
        _validate_cross_references(config, "config.yaml")


def test_unknown_alerter_rejected():
    from pathlib import Path

    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[[35.72, -78.70], [35.73, -78.70], [35.73, -78.69]],
                rules=[
                    make_rule(
                        name="warn",
                    )
                ],
            )
        }
    )
    config.zones["pad"].rules[0].on_activate = [
        AlertActionConfig(method="discord", options={})
    ]
    with pytest.raises(ConfigError, match="unknown alerter"):
        _validate_cross_references(config, Path("config.yaml"))


def test_webhook_url_rules():
    assert webhook_url_allowed("https://example.com/hook")
    assert webhook_url_allowed("http://localhost:8080/hook")
    assert webhook_url_allowed("http://127.0.0.1/hook")
    assert webhook_url_allowed("http://[::1]/hook")
    assert not webhook_url_allowed("http://169.254.169.254/")
    assert not webhook_url_allowed("ftp://example.com")
    assert not webhook_url_allowed("http://localhost.attacker.example/hook")
    assert not webhook_url_allowed("http://127.0.0.1.attacker.example/hook")
    assert not webhook_url_allowed("http://localhost@evil.example/hook")
    assert not webhook_url_allowed("http://")


_MINIMAL_YAML = """
home:
  latitude: 35.7275
  longitude: -78.6959
receivers:
  main:
    type: mock
"""


def test_env_interpolation_default(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
database:
  uri: "${PYAERIAL_TEST_MONGO:-mongodb://localhost:27017}"
"""
    )
    config = load_config(cfg_path)
    assert config.database.uri == "mongodb://localhost:27017"


def test_env_interpolation_webhook_url(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_WEBHOOK_URL", "https://hooks.example/a")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
zones:
  pad:
    coordinates: [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69], [35.72, -78.70]]
    rules:
      - name: warn
        when:
          altitude: { max: 2000 }
        dwell_seconds: 1
        on_activate:
          - method: webhook
            options:
              url: "${PYAERIAL_WEBHOOK_URL}"
"""
    )
    config = load_config(cfg_path)
    url = config.zones["pad"].rules[0].on_activate[0].options["url"]
    assert url == "https://hooks.example/a"


def test_env_interpolation_missing_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("PYAERIAL_MISSING_SECRET", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
database:
  uri: "${PYAERIAL_MISSING_SECRET}"
"""
    )
    with pytest.raises(ConfigError, match="unset environment variable"):
        load_config(cfg_path)
