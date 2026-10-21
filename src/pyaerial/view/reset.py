"""History and live cache purge operations."""

from __future__ import annotations

import sys
import time
from typing import Any, Literal

from pyaerial.config import load_config
from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS
from pyaerial.store.history import HistoryStore, HistoryUnavailable
from pyaerial.view.store import open_live_session

EngineState = Literal["live", "stopped", "unknown"]


def engine_is_live(live_store: Any, now: float | None = None) -> bool:
    getter = getattr(live_store, "engine_seen_at", None)
    seen = getter() if callable(getter) else None
    if not isinstance(seen, (int, float)):
        return False
    return (now if now is not None else time.time()) - seen < LIVE_ENGINE_TTL_SECONDS


def engine_state(live_store: Any) -> EngineState:
    """Whether the tracking engine is running, per the live store.

    "unknown" when the store cannot be reached: no heartbeat is *not* proof
    that the engine is stopped if Redis itself is down.
    """
    if live_store is None:
        return "stopped"
    ping = getattr(live_store, "ping", None)
    if callable(ping) and not ping():
        return "unknown"
    return "live" if engine_is_live(live_store) else "stopped"


_UNKNOWN_ENGINE_MSG = (
    "Cannot reach the live store (Redis) to confirm the tracking engine is "
    "stopped; refusing to delete history it may rewrite. Start Redis, or "
    "re-run with --force if you are sure `pyaerial run` is not running."
)


def run_reset(
    config_path: str,
    *,
    icao: str | None = None,
    yes: bool = False,
    force: bool = False,
) -> None:
    config = load_config(config_path)
    history = HistoryStore(config.database.path)
    live_store = open_live_session(config)
    try:
        code = _reset(history, live_store, icao=icao, yes=yes, force=force)
    finally:
        live_store.close()
        history.close()
    if code:
        sys.exit(code)


def reset_history(
    history: HistoryStore | None,
    live_store: Any = None,
    *,
    icao: str | None = None,
    yes: bool = False,
    force: bool = False,
) -> int:
    """Purge retained SQLite flights and stopped-engine Redis keys. Returns process exit code.

    ``force`` proceeds when the engine state cannot be determined (Redis
    unreachable); it never overrides a confirmed running engine.
    """
    return _reset(history, live_store, icao=icao, yes=yes, force=force)


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Refusing to reset without --yes (stdin is not a terminal).",
            file=sys.stderr,
        )
        return False
    print(prompt)
    try:
        answer = input("Type 'yes' to continue: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.", file=sys.stderr)
        return False
    if answer != "yes":
        print("Aborted.", file=sys.stderr)
        return False
    return True


def _reset(
    history: HistoryStore | None,
    live_store: Any,
    *,
    icao: str | None,
    yes: bool,
    force: bool = False,
) -> int:
    target = icao.lower().strip() if icao else ""
    if target:
        prompt = (
            f"This will delete retained history for ICAO {target}. "
            "Live Redis is left alone if the tracking engine is running."
        )
    else:
        prompt = (
            "This will delete all retained flights, tracks, and alerts. "
            "Live Redis is left alone if the tracking engine is running."
        )
    if not _confirm(prompt, yes):
        return 1

    state = engine_state(live_store)
    if state == "unknown" and not force:
        print(_UNKNOWN_ENGINE_MSG, file=sys.stderr)
        return 1
    if target:
        return _reset_icao(history, live_store, target, state)
    return _reset_all(history, live_store, state)


def _history_error(exc: HistoryUnavailable) -> int:
    print(f"History reset failed: {exc}", file=sys.stderr)
    return 1


def _reset_all(
    history: HistoryStore | None, live_store: Any, state: EngineState
) -> int:
    if state == "live":
        print(
            "Tracking engine is running; refusing to delete history that would "
            "be rewritten on the next expire. Stop `pyaerial run` first.",
            file=sys.stderr,
        )
        return 1
    if history is not None:
        try:
            reset = history.reset_all()
        except HistoryUnavailable as exc:
            return _history_error(exc)
        if not reset:
            print(
                "History database is disconnected; nothing was reset.",
                file=sys.stderr,
            )
            return 1
    if state == "unknown":
        print("History reset. Live Redis was unreachable and was not cleared.")
        return 0
    if live_store is not None and hasattr(live_store, "clear_all"):
        live_store.clear_all()
        print("Database reset. Dropped all planes and flights.")
        return 0
    print("History reset.")
    return 0


def _reset_icao(
    history: HistoryStore | None, live_store: Any, target: str, state: EngineState
) -> int:
    if state == "live":
        live_ids = [
            flight.get("flight_id")
            for flight in (getattr(live_store, "get_flights", lambda: [])() or [])
            if str(flight.get("icao", "")).lower() == target
        ]
        if any(live_ids):
            print(
                f"Tracking engine is running and {target} is still live; "
                "refusing to delete history that would be rewritten. "
                "Stop `pyaerial run` first.",
                file=sys.stderr,
            )
            return 1
    if history is not None:
        try:
            deleted = history.delete_icao(target)
        except HistoryUnavailable as exc:
            return _history_error(exc)
        if not deleted:
            print(
                f"History database is disconnected; plane {target} was not deleted.",
                file=sys.stderr,
            )
            return 1
    if live_store is not None and state != "unknown":
        _drop_live_icao(live_store, target)
    print(f"Dropped plane {target}.")
    return 0


def _drop_live_icao(live_store: Any, target: str) -> None:
    if not hasattr(live_store, "get_flights") or not hasattr(live_store, "pop_flight"):
        return
    flight_ids = [
        flight.get("flight_id")
        for flight in (live_store.get_flights() or [])
        if str(flight.get("icao", "")).lower() == target
    ]
    for flight_id in flight_ids:
        if flight_id:
            live_store.pop_flight(flight_id)
