"""Redis password from a file: PYAERIAL_REDIS_PASSWORD_FILE and
database.redis_password_file."""

from __future__ import annotations

import logging

import pytest
from redis.connection import parse_url

from pyaerial.config.loader import ConfigError, load_config
from pyaerial.urls import redact_uri

_YAML = """
home: {latitude: 35.7, longitude: -78.6}
receivers: {main: {type: dump1090}}
"""


SECRET = "p@ss:w/rd#?%+ end"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("PYAERIAL_REDIS", "PYAERIAL_REDIS_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)


def _config(tmp_path, extra: str = ""):
    path = tmp_path / "config.yaml"
    path.write_text(_YAML + extra)
    return path


def test_env_password_file_is_applied_to_the_uri(tmp_path, monkeypatch):
    secret = tmp_path / "redis_password"
    secret.write_text(SECRET + "\n")  # secret files usually end in a newline
    monkeypatch.setenv("PYAERIAL_REDIS", "redis://redis:6379/0")
    monkeypatch.setenv("PYAERIAL_REDIS_PASSWORD_FILE", str(secret))
    config = load_config(_config(tmp_path))
    parsed = parse_url(config.database.redis_uri)
    assert parsed["password"] == SECRET
    assert parsed["host"] == "redis" and parsed["db"] == 0
    # Safe to log, and kept out of the model's repr.
    assert "p%40ss" not in redact_uri(config.database.redis_uri)
    assert "p%40ss" not in repr(config) and SECRET not in repr(config)


def test_config_key_resolves_relative_to_the_config_file(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "redis").write_text("hunter2")
    config = load_config(
        _config(
            tmp_path,
            "database:\n  redis_uri: unix:///run/redis.sock?db=2\n"
            "  redis_password_file: secrets/redis\n",
        )
    )
    parsed = parse_url(config.database.redis_uri)
    assert parsed["password"] == "hunter2"
    assert parsed["path"] == "/run/redis.sock" and parsed["db"] == 2


def test_username_in_uri_is_kept(tmp_path, monkeypatch):
    secret = tmp_path / "pw"
    secret.write_text("hunter2")
    monkeypatch.setenv("PYAERIAL_REDIS", "rediss://pyaerial@cache:6380/1")
    monkeypatch.setenv("PYAERIAL_REDIS_PASSWORD_FILE", str(secret))
    parsed = parse_url(load_config(_config(tmp_path)).database.redis_uri)
    assert (parsed["username"], parsed["password"], parsed["port"]) == (
        "pyaerial",
        "hunter2",
        6380,
    )


@pytest.mark.parametrize(
    ("content", "uri", "message"),
    [
        (None, "redis://redis:6379/0", "cannot read"),
        ("", "redis://redis:6379/0", "is empty"),
        ("two\nlines", "redis://redis:6379/0", "one line"),
        ("hunter2", "redis://:INLINE-SECRET@redis:6379/0", "not both"),
    ],
)
def test_password_file_errors_never_echo_secrets(
    tmp_path, monkeypatch, caplog, content, uri, message
):
    secret = tmp_path / "pw"
    if content is not None:
        secret.write_text(content)
    monkeypatch.setenv("PYAERIAL_REDIS", uri)
    monkeypatch.setenv("PYAERIAL_REDIS_PASSWORD_FILE", str(secret))
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(ConfigError, match=message) as exc,
    ):
        load_config(_config(tmp_path))
    text = str(exc.value) + caplog.text
    assert "PYAERIAL_REDIS_PASSWORD_FILE" in str(
        exc.value
    ) or "redis_password_file" in str(exc.value)
    for leaked in ("hunter2", "INLINE-SECRET", "two\nlines"):
        assert leaked not in text
