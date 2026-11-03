from __future__ import annotations

import logging

import pytest
import redis

from fake_redis import FakeRedis
from helpers import make_config
from pyaerial.config.schema import Config


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def fake(monkeypatch) -> FakeRedis:
    """Every ``redis.Redis.from_url`` in the test returns this in-process fake."""
    client = FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: client))
    return client


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Undo root-logger handlers a test installs (e.g. ``cli.main`` runs
    ``setup_logging``): they write to that test's captured stderr, which is
    closed once the test ends, so later background-thread logging would fail."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        ours = type(handler).__module__.startswith("_pytest")
        if handler not in handlers and not ours:
            root.removeHandler(handler)
    root.setLevel(level)
