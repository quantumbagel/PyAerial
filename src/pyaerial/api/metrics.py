"""Prometheus ``/metrics`` for the portal (text exposition format 0.0.4).

Hand-rolled: the format is a few lines of text and the portal only exports
gauges and counters. Engine numbers come from the ``live:engine`` heartbeat
(one Redis GET per scrape); portal numbers are read on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response

from pyaerial import __version__
from pyaerial.api.beast import BeastHub
from pyaerial.api.broadcaster import LiveBroadcaster
from pyaerial.api.queries import engine_is_live
from pyaerial.api.security import origin_allowed
from pyaerial.config.schema import Config

log = logging.getLogger("pyaerial.webapp")

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
#: Every WebSocket endpoint, labelled by its canonical path (``/ws`` is ``/ws/live``).
WS_ENDPOINTS = ("/ws/live", "/ws/raw", "/ws/beast")
#: Drop reasons always exported (at 0 until they happen). /ws/live:
#: ``client_too_slow`` (unread RPC responses), ``server_budget`` (largest
#: backlog over the server-wide budget); /ws/raw and /ws/beast: ``feed_behind``.
WS_DROP_REASONS = ("client_too_slow", "server_budget", "feed_behind")
#: Beast TCP: ``feed_behind`` (queue full too long), ``not_reading`` (drain timeout).
TCP_DROP_REASONS = ("feed_behind", "not_reading")

_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

Labels = Mapping[str, str]
Sample = tuple[Labels, float]

# Heartbeat ``counters`` exported as ``pyaerial_engine_<key>_total``.
_ENGINE_COUNTERS = (
    ("frames_received", "Frames taken off the receiver intake queue."),
    ("frames_dropped", "Frames discarded because the intake queue was full."),
    ("messages_processed", "Mode S messages decoded by the tracker."),
    (
        "pending_evicted",
        "Deferred flights dropped from memory while the archive was down.",
    ),
)


# --- Exposition format --------------------------------------------------------


def _escape_help(text: str) -> str:
    return text.replace("\\", r"\\").replace("\n", r"\n")


def escape_label_value(value: str) -> str:
    return value.replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")


def format_value(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value.is_integer() and abs(value) < 2**53:
        return str(int(value))
    return repr(value)


class Exposition:
    """Collects metric families and renders them as exposition text."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._names: set[str] = set()

    def add(self, name: str, kind: str, help_text: str, samples: Iterable[Sample]) -> None:
        """One family. A family without samples is left out entirely."""
        if not _METRIC_NAME_RE.match(name) or not name.startswith("pyaerial_"):
            raise ValueError(f"invalid metric name {name!r}")
        if kind not in ("counter", "gauge"):
            raise ValueError(f"unsupported metric type {kind!r}")
        if kind == "counter" and not name.endswith("_total"):
            raise ValueError(f"counter {name!r} must end in _total")
        if name in self._names:
            raise ValueError(f"duplicate metric family {name!r}")
        rows = list(samples)
        if not rows:
            return
        self._names.add(name)
        self._lines.append(f"# HELP {name} {_escape_help(help_text)}")
        self._lines.append(f"# TYPE {name} {kind}")
        for labels, value in rows:
            self._lines.append(f"{name}{_format_labels(labels)} {format_value(value)}")

    def gauge(self, name: str, help_text: str, value: float, **labels: str) -> None:
        self.add(name, "gauge", help_text, [(labels, value)])

    def counter(self, name: str, help_text: str, value: float) -> None:
        self.add(name, "counter", help_text, [({}, value)])

    def text(self) -> str:
        return "\n".join(self._lines) + "\n" if self._lines else ""


def _format_labels(labels: Labels) -> str:
    if not labels:
        return ""
    parts = []
    for key, value in labels.items():
        if not _LABEL_NAME_RE.match(key) or key.startswith("__"):
            raise ValueError(f"invalid label name {key!r}")
        parts.append(f'{key}="{escape_label_value(str(value))}"')
    return "{" + ",".join(parts) + "}"


# --- Inputs -------------------------------------------------------------------


class WebSocketStats:
    """Open WebSocket connections per endpoint, and refused RPC requests.

    :meth:`track` wraps the route handlers at registration, so refused
    connections (origin, connection caps) are counted for the instant it
    takes to close them. Event loop only.
    """

    def __init__(self) -> None:
        self._open: dict[str, int] = dict.fromkeys(WS_ENDPOINTS, 0)
        #: /ws/live requests answered ``rate_limited`` (delayed ones not counted).
        self.rate_limited = 0

    def track(
        self, endpoint: str, handler: Callable[[WebSocket], Awaitable[None]]
    ) -> Callable[[WebSocket], Awaitable[None]]:
        if endpoint not in self._open:
            raise ValueError(f"unknown WebSocket endpoint {endpoint!r}")

        async def tracked(websocket: WebSocket) -> None:
            self._open[endpoint] += 1
            try:
                await handler(websocket)
            finally:
                self._open[endpoint] -= 1

        return tracked

    def snapshot(self) -> dict[str, int]:
        return dict(self._open)


@dataclass(frozen=True, slots=True)
class PortalCounts:
    websockets: Mapping[str, int]
    beast_tcp_clients: int
    queued_bytes: int
    websocket_drops: Mapping[str, int] = field(default_factory=dict)
    beast_tcp_drops: Mapping[str, int] = field(default_factory=dict)
    rate_limited: int = 0


@dataclass(frozen=True, slots=True)
class Backends:
    redis_up: bool
    archive_up: bool
    #: The ``live:engine`` document, or None (missing, unreadable, Redis down).
    heartbeat: dict[str, Any] | None
    now: float


class _SeenAt:
    """Lets :func:`queries.engine_is_live` judge a heartbeat already read."""

    def __init__(self, seen_at: Any) -> None:
        self._seen_at = seen_at

    def engine_seen_at(self) -> Any:
        return self._seen_at


def _read_heartbeat(live_store: Any) -> tuple[bool, dict[str, Any] | None]:
    """``(redis_up, heartbeat)`` with one GET (plus /ready's throttled PING)."""
    try:
        redis_up = bool(live_store.ping())
    except Exception as exc:
        log.debug("metrics: live store ping failed: %r", exc)
        return False, None
    if not redis_up:
        return False, None
    read = getattr(live_store, "engine_heartbeat", None)
    if not callable(read):
        # Duck-typed stores: only the timestamp is available.
        getter = getattr(live_store, "engine_seen_at", None)
        seen = getter() if callable(getter) else None
        return True, ({"seen_at": seen} if seen is not None else None)
    try:
        return True, read()
    except Exception as exc:
        log.debug("metrics: heartbeat read failed: %r", exc)
        return False, None


def probe_backends(live_store: Any | None, history: Any | None) -> Backends:
    """Blocking Redis and SQLite checks; run off the event loop."""
    if live_store is None:
        redis_up, heartbeat = False, None
    else:
        redis_up, heartbeat = _read_heartbeat(live_store)
    archive_up = False
    if history is not None:
        try:
            archive_up = bool(history.ping())
        except Exception as exc:
            log.debug("metrics: archive ping failed: %r", exc)
    return Backends(redis_up, archive_up, heartbeat, time.time())


# --- Rendering ----------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _labelled(values: Any, label: str, always: Iterable[str] = ()) -> list[Sample]:
    """Samples from a ``{name: count}`` heartbeat map; ``always`` names start at 0."""
    counts: dict[str, float] = dict.fromkeys(always, 0)
    if isinstance(values, dict):
        for key, raw in values.items():
            number = _number(raw)
            if isinstance(key, str) and number is not None:
                counts[key] = number
    return [({label: key}, counts[key]) for key in sorted(counts)]


def _engine(out: Exposition, backends: Backends, receivers: Iterable[str]) -> None:
    doc = backends.heartbeat
    seen = doc.get("seen_at") if doc is not None else None
    live = engine_is_live(_SeenAt(seen), now=backends.now)
    out.gauge(
        "pyaerial_engine_up",
        "1 while the engine heartbeat in Redis is fresh (the rule /ready uses), "
        "else 0. Other pyaerial_engine_ metrics are omitted while 0.",
        1 if live else 0,
    )
    if not live or doc is None:
        return
    counters = doc.get("counters")
    if not isinstance(counters, dict):
        counters = {}
    since = _number(counters.get("since"))
    if since is not None:
        out.gauge(
            "pyaerial_engine_start_time_seconds",
            "Engine start (unix seconds); the engine counters count from here.",
            since,
        )
    for key, help_text in _ENGINE_COUNTERS:
        value = _number(counters.get(key))
        if value is not None:
            out.counter(f"pyaerial_engine_{key}_total", help_text, value)
    out.add(
        "pyaerial_engine_receiver_restarts_total",
        "counter",
        "Receiver restarts after a receiver stopped, per configured receiver.",
        _labelled(counters.get("receiver_restarts"), "receiver", receivers),
    )
    out.add(
        "pyaerial_engine_stage_errors_total",
        "counter",
        "Exceptions caught in an engine tick stage, per stage.",
        _labelled(counters.get("stage_errors"), "stage"),
    )
    planes = _number(counters.get("planes_tracked"))
    if planes is not None:
        out.gauge("pyaerial_engine_planes_tracked", "Aircraft tracked now.", planes)
    pending = _number(doc.get("history_pending"))
    if pending is not None:
        out.gauge(
            "pyaerial_engine_history_pending",
            "Finished flights waiting in engine memory to be archived.",
            pending,
        )
    state = doc.get("history")
    if isinstance(state, str):
        out.gauge(
            "pyaerial_engine_history_up",
            "1 while the engine can write the archive (history: ok); "
            "0 when it is down or disabled.",
            1 if state == "ok" else 0,
        )


def render(
    *,
    backends: Backends,
    portal: PortalCounts,
    receivers: Iterable[str] = (),
    version: str = __version__,
) -> str:
    out = Exposition()
    out.gauge(
        "pyaerial_build_info", "PyAerial version of the portal; always 1.", 1, version=version
    )
    out.gauge(
        "pyaerial_redis_up",
        "1 while the portal can read Redis (as in /ready), else 0.",
        1 if backends.redis_up else 0,
    )
    out.gauge(
        "pyaerial_archive_up",
        "1 while the portal can read the SQLite archive (as in /ready), else 0.",
        1 if backends.archive_up else 0,
    )
    _engine(out, backends, receivers)
    out.add(
        "pyaerial_websocket_connections",
        "gauge",
        "Open WebSocket connections per endpoint (/ws counts as /ws/live).",
        [({"endpoint": name}, portal.websockets.get(name, 0)) for name in WS_ENDPOINTS],
    )
    out.gauge(
        "pyaerial_beast_tcp_clients",
        "Connected Beast TCP feeders.",
        portal.beast_tcp_clients,
    )
    out.gauge(
        "pyaerial_websocket_queued_bytes",
        "Bytes of per-client /ws/live messages (RPC responses, telemetry) queued "
        "or being sent, all clients; shared snapshots are not counted.",
        portal.queued_bytes,
    )
    out.add(
        "pyaerial_websocket_clients_dropped_total",
        "counter",
        "WebSocket clients disconnected for backpressure (close 1013): "
        "client_too_slow and server_budget on /ws/live, feed_behind on "
        "/ws/raw and /ws/beast.",
        _labelled(dict(portal.websocket_drops), "reason", WS_DROP_REASONS),
    )
    out.add(
        "pyaerial_beast_tcp_clients_dropped_total",
        "counter",
        "Beast TCP feeders dropped: feed_behind (fell 10 s behind) or "
        "not_reading (socket accepted nothing).",
        _labelled(dict(portal.beast_tcp_drops), "reason", TCP_DROP_REASONS),
    )
    out.counter(
        "pyaerial_websocket_rate_limited_total",
        "/ws/live RPC requests refused with rate_limited (delayed requests "
        "are not counted).",
        portal.rate_limited,
    )
    return out.text()


# --- Route --------------------------------------------------------------------


def add_metrics_route(
    app: FastAPI,
    *,
    config: Config,
    history: Any | None,
    live_store: Any | None,
    broadcaster: LiveBroadcaster,
    beast_hub: BeastHub,
    websockets: WebSocketStats,
) -> None:
    """Register ``GET /metrics`` (an explicit 404 when ``web.metrics`` is off)."""
    web = config.web
    # Names only: receiver hosts and ports are never exported.
    receivers = tuple(config.receivers)

    if not web.metrics:

        @app.get("/metrics", include_in_schema=False)
        def metrics_disabled() -> Response:
            # Otherwise the SPA fallback would answer with index.html.
            raise HTTPException(404)

        return

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        # Host: TrustedHostMiddleware, as for every route. A browser Origin
        # must pass the WebSocket rule; scrapers send none.
        origin = request.headers.get("origin")
        if not origin_allowed(
            origin,
            web.origins,
            host_header=request.headers.get("host"),
            allow_localhost=web.allow_localhost_origins,
            allowed_hosts=web.allowed_hosts,
        ):
            return Response("origin not allowed\n", status_code=403, media_type="text/plain")
        portal = PortalCounts(
            websockets=websockets.snapshot(),
            beast_tcp_clients=beast_hub.tcp_client_count,
            queued_bytes=broadcaster.queued_bytes,
            websocket_drops=Counter(broadcaster.dropped_clients)
            + Counter(beast_hub.websocket_drops),
            beast_tcp_drops=beast_hub.tcp_drops,
            rate_limited=websockets.rate_limited,
        )
        backends = await asyncio.to_thread(probe_backends, live_store, history)
        return Response(
            render(backends=backends, portal=portal, receivers=receivers),
            media_type=CONTENT_TYPE,
            headers={"Cache-Control": "no-store"},
        )
