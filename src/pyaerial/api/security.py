"""Origin checks, security headers, and WebSocket admission limits."""

from __future__ import annotations

import ipaddress
import re
import time
from collections import Counter
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_DEFAULT_PORTS = {"http": 80, "https": 443}
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SAFE_HOST_RE = re.compile(
    r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$"
)

Origin = tuple[str, str, int]


def parse_origin(value: str | None) -> Origin | None:
    """``(scheme, host, port)`` for an ``http(s)://host[:port]`` origin, else None."""
    if not value:
        return None
    try:
        parsed = urlparse(value.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.path not in ("", "/"):
        return None
    return scheme, host.lower(), port or _DEFAULT_PORTS[scheme]


def _parse_host_header(value: str | None) -> tuple[str, int | None] | None:
    if not value:
        return None
    text = value.split(",")[0].strip()
    try:
        parsed = urlparse(f"//{text}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    return host.lower(), port


def host_trusted(host: str, allowed_hosts: list[str] | None = None) -> bool:
    """Whether a ``Host`` name can be trusted for same-origin checks.

    DNS rebinding always arrives with the attacker's hostname, so IP literals
    and loopback names are safe; any other name must be listed explicitly.
    """
    if host in _LOCAL_HOSTS:
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    for raw in allowed_hosts or ():
        entry = raw.lower()
        if entry == "*" or entry == host:
            return True
        if entry.startswith("*.") and host.endswith(entry[1:]):
            return True
    return False


def origin_allowed(
    origin: str | None,
    allowed: list[str] | None = None,
    *,
    host_header: str | None = None,
    allow_localhost: bool = False,
    allowed_hosts: list[str] | None = None,
) -> bool:
    """Decide whether a browser ``Origin`` may open a WebSocket.

    * No ``Origin`` header: a native (non-browser) client; allowed.
    * ``"*"`` in ``allowed``: any origin.
    * Exact ``scheme://host:port`` match against ``allowed``.
    * Same origin: the Origin's host and port equal the request ``Host``, and
      that host is trusted (see :func:`host_trusted`).
    * ``allow_localhost``: any http(s) origin on localhost / 127.0.0.1 / [::1].
    """
    if not origin:
        return True
    entries = list(allowed or [])
    if "*" in entries:
        return True
    parsed = parse_origin(origin)
    if parsed is None:
        return False
    scheme, host, port = parsed
    if allow_localhost and host in _LOCAL_HOSTS:
        return True
    for entry in entries:
        if parse_origin(entry) == parsed:
            return True
    request_host = _parse_host_header(host_header)
    if request_host is not None:
        req_host, req_port = request_host
        if (
            req_host == host
            and (req_port or _DEFAULT_PORTS[scheme]) == port
            and host_trusted(host, allowed_hosts)
        ):
            return True
    return False


def localhost_origin_regex() -> str:
    return r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


# Everything the built portal loads: its own bundle (system fonts only),
# OpenFreeMap vector style/tiles/glyphs/sprites (MapView.tsx), MapLibre's blob:
# web worker, and aircraft photo thumbnails (https only; see payloads.https_url).
# Leaflet/plane icons set inline style attributes, hence 'unsafe-inline' styles.
_CSP_BASE = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob: https:",
    "worker-src 'self' blob:",
    "child-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)
_CONNECT_SRC = "connect-src 'self' https://tiles.openfreemap.org"


def content_security_policy(host: str | None = None) -> str:
    """CSP for portal responses. ``host`` adds explicit ws:/wss: for old Safari."""
    connect = _CONNECT_SRC
    if host and _SAFE_HOST_RE.match(host):
        connect += f" ws://{host} wss://{host}"
    return "; ".join((*_CSP_BASE, connect))


class SecurityHeadersMiddleware:
    """Add CSP, nosniff, referrer and framing headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault(
                    "Content-Security-Policy", content_security_policy(host)
                )
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ConnectionLimiter:
    """Cap concurrent WebSocket connections in total and per client IP.

    Used only from the event loop, so no locking is needed.
    """

    def __init__(self, max_total: int, max_per_ip: int) -> None:
        self.max_total = max_total
        self.max_per_ip = max_per_ip
        self._total = 0
        self._per_ip: Counter[str] = Counter()

    @property
    def total(self) -> int:
        return self._total

    def acquire(self, ip: str) -> str | None:
        """Reserve a slot; return a rejection reason when full."""
        if self._total >= self.max_total:
            return "server connection limit reached"
        if self._per_ip[ip] >= self.max_per_ip:
            return "per-client connection limit reached"
        self._total += 1
        self._per_ip[ip] += 1
        return None

    def count(self, ip: str) -> int:
        """Open connections from ``ip``."""
        return self._per_ip.get(ip, 0)

    def release(self, ip: str) -> None:
        if self._per_ip[ip] > 0:
            self._per_ip[ip] -= 1
            self._total = max(0, self._total - 1)
        if self._per_ip[ip] <= 0:
            self._per_ip.pop(ip, None)


class TokenBucket:
    """Simple token bucket: ``rate`` tokens/second up to ``burst``."""

    def __init__(
        self,
        rate: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = float(rate)
        self.burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)

    def full(self) -> bool:
        """Refilled to the full burst (no debt, nothing spent)."""
        self._refill()
        return self._tokens >= self.burst

    def take(self) -> bool:
        """Consume a token if one is available right now."""
        self._refill()
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def reserve(self, max_wait: float) -> float | None:
        """Claim the next token: seconds to wait first, or None if > ``max_wait``.

        A granted reservation may leave the bucket in debt; the caller sleeps
        the returned delay before acting, which throttles to ``rate``.
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        delay = (1.0 - self._tokens) / self.rate
        if delay > max_wait:
            return None
        self._tokens -= 1.0
        return delay


class BucketRegistry:
    """Per-key token buckets that outlive the connections using them.

    Dropping a bucket when its client disconnects would let the client reset
    its burst and any throttle debt by reconnecting. A bucket is evicted only
    once nothing uses it and it has refilled (it then holds no state a new
    bucket would not). At ``max_entries`` the least recently used idle bucket
    goes even if not refilled, so the map stays bounded. Event loop only.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        max_entries: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = rate
        self.burst = burst
        self.max_entries = max_entries
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._users: Counter[str] = Counter()
        self._last_used: dict[str, float] = {}

    def __len__(self) -> int:
        return len(self._buckets)

    def __contains__(self, key: object) -> bool:
        return key in self._buckets

    def acquire(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_entries:
                self.sweep()
                self._evict_lru()
            bucket = self._buckets[key] = TokenBucket(
                self.rate, self.burst, clock=self._clock
            )
        self._users[key] += 1
        self._last_used[key] = self._clock()
        return bucket

    def release(self, key: str) -> None:
        if self._users[key] > 0:
            self._users[key] -= 1
        if self._users[key] <= 0:
            self._users.pop(key, None)
        self._last_used[key] = self._clock()
        bucket = self._buckets.get(key)
        if bucket is not None and key not in self._users and bucket.full():
            self._forget(key)

    def sweep(self) -> None:
        """Evict idle buckets that have refilled."""
        for key in [k for k, b in self._buckets.items() if k not in self._users and b.full()]:
            self._forget(key)

    def _evict_lru(self) -> None:
        while len(self._buckets) >= self.max_entries:
            idle = [k for k in self._buckets if k not in self._users]
            if not idle:
                return  # every bucket is in use: bounded by the connection caps
            self._forget(min(idle, key=lambda k: self._last_used.get(k, 0.0)))

    def _forget(self, key: str) -> None:
        self._buckets.pop(key, None)
        self._last_used.pop(key, None)


def client_ip(connection: Any) -> str:
    client = getattr(connection, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "unknown"
