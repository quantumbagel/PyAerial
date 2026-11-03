from __future__ import annotations

import logging
from urllib.parse import urlsplit

import pytest
import redis
import requests.sessions

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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@pytest.fixture(autouse=True, scope="session")
def _no_outbound_http():
    """Tests never reach the network through ``requests``.

    An engine or PlaneCalculator in a test looks aircraft up on hexdb.io and
    Planespotters from its ``callsign-lookup-*`` threads (``AircraftDB`` via
    ``requests.get``), which really went out. Session-scoped, so a lookup
    thread that outlives its test is blocked too. Loopback still works (the
    tests' own HTTP servers). Tests of enrichment patch ``requests.get``
    with their own fakes, which take effect before this layer.
    """
    real_request = requests.sessions.Session.request

    def request(self, method, url, *args, **kwargs):
        host = (urlsplit(str(url)).hostname or "").lower()
        if host not in _LOOPBACK_HOSTS:
            raise requests.ConnectionError(
                f"tests must not reach the network: {method} {url}"
            )
        return real_request(self, method, url, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(requests.sessions.Session, "request", request)
        yield


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
