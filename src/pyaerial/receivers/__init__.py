"""
Receiver plugins: sources of raw ADS-B / Mode S messages.

A receiver runs in its own thread, pulls raw hex messages from some transport,
and emits ``(hex, timestamp)`` pairs via the ``emit`` callback given to it. New
receivers register themselves with :func:`register_receiver` and are then
selectable by name from the configuration.
"""

from __future__ import annotations

import abc
import logging
import threading
from typing import Callable

Emit = Callable[[str, float], None]

_REGISTRY: dict[str, type["Receiver"]] = {}


class Receiver(abc.ABC):
    """Base class for all receivers."""

    def __init__(self, name: str, emit: Emit, arguments: dict):
        self.name = name
        self.emit = emit
        self.arguments = arguments
        self.log = logging.getLogger(f"pyaerial.receiver.{name}")
        self._stop = threading.Event()
        self.configure(arguments)

    def configure(self, arguments: dict) -> None:
        """Validate/store receiver-specific arguments. Override as needed."""

    @abc.abstractmethod
    def run(self) -> str | None:
        """
        Blocking loop that emits messages until the receiver stops or fails.

        Implementations must periodically check :meth:`should_stop` and return a
        human-readable reason string when they exit (or ``None`` for a clean,
        requested stop).
        """

    def stop(self) -> None:
        self._stop.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()


def register_receiver(name: str) -> Callable[[type[Receiver]], type[Receiver]]:
    """Class decorator that registers a receiver under ``name``."""

    def decorator(cls: type[Receiver]) -> type[Receiver]:
        _REGISTRY[name] = cls
        return cls

    return decorator


def available_receivers() -> list[str]:
    return sorted(_REGISTRY)


def create_receiver(method: str, name: str, emit: Emit, arguments: dict) -> Receiver:
    """Instantiate the receiver registered under ``method``."""
    if method not in _REGISTRY:
        raise KeyError(
            f"unknown receiver method {method!r}; available: {available_receivers()}"
        )
    return _REGISTRY[method](name, emit, arguments)


def register_builtins() -> None:
    """Import built-in receivers so they register themselves."""
    from pyaerial.receivers import dump1090 as _dump1090  # noqa: F401
    from pyaerial.receivers import mock as _mock  # noqa: F401
    from pyaerial.receivers import replay as _replay  # noqa: F401

    try:  # pyrtlsdr / librtlsdr may be unavailable on some systems.
        from pyaerial.receivers import py1090 as _py1090  # noqa: F401
    except Exception as exc:  # pragma: no cover - optional dependency
        logging.getLogger("pyaerial.receiver").debug(
            "py1090 receiver unavailable: %s", exc
        )


register_builtins()
