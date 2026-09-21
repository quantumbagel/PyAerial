"""History and live cache purge operations."""

from __future__ import annotations

import sys
import time
from typing import Any

from pyaerial.config import load_config
from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS
from pyaerial.store.history import HistoryStore
from pyaerial.view.store import open_live_session


def engine_is_live(live_store: Any, now: float | None = None) -> bool:
    getter = getattr(live_store, "engine_seen_at", None)
    seen = getter() if callable(getter) else None
    if not isinstance(seen, (int, float)):
        return False
    return (now if now is not None else time.time()) - seen < LIVE_ENGINE_TTL_SECONDS


def run_reset(
    config_path: str,
    *,
    icao: str | None = None,
    yes: bool = False,
) -> None:
    config = load_config(config_path)
    history = HistoryStore(config.database.path)
    live_store = open_live_session(config)
    try:
        code = _reset(history, live_store, icao=icao, yes=yes)
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
) -> int:
    """Purge retained SQLite flights and stopped-engine Redis keys. Returns process exit code."""
    return _reset(history, live_store, icao=icao, yes=yes)


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

    if target:
        return _reset_icao(history, live_store, target)
    return _reset_all(history, live_store)


def _reset_all(history: HistoryStore | None, live_store: Any) -> int:
    if live_store is not None and engine_is_live(live_store):
        print(
            "Tracking engine is running; refusing to delete history that would "
            "be rewritten on the next expire. Stop `pyaerial run` first.",
            file=sys.stderr,
        )
        return 1
    if history is not None:
        if not history.reset_all():
            print(
                "History database is disconnected; nothing was reset.",
                file=sys.stderr,
            )
            return 1
    if live_store is not None and hasattr(live_store, "clear_all"):
        live_store.clear_all()
        print("Database reset. Dropped all planes and flights.")
        return 0
    print("History reset.")
    return 0


def _reset_icao(
    history: HistoryStore | None, live_store: Any, target: str
) -> int:
    if live_store is not None and engine_is_live(live_store):
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
        if not history.delete_icao(target):
            print(
                f"History database is disconnected; plane {target} was not deleted.",
                file=sys.stderr,
            )
            return 1
    if live_store is not None:
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
