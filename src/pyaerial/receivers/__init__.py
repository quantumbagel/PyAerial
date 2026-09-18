"""
Receiver plugins: sources of raw ADS-B / Mode S messages.

A receiver runs in its own thread, pulls raw hex messages from some transport,
and emits hex frames via the ``emit`` callback given to it (optional ``rssi``
and ``clock`` keyword arguments when the transport provides them). New
receivers register themselves with :func:`register_receiver` and are then
selectable by name from the configuration.
"""

from __future__ import annotations

import abc
import logging
import threading
from typing import Callable, Protocol

from pyaerial.receivers.frames import RawFrame

__all__ = [
    "Emit",
    "RawFrame",
    "Receiver",
    "available_receivers",
    "create_receiver",
    "register_builtins",
    "register_receiver",
    "unavailable_receivers",
]


class Emit(Protocol):
    """Callback used by receivers to hand a frame to the engine."""

    def __call__(
        self,
        msg_hex: str,
        timestamp: float,
        *,
        rssi: float | None = None,
        clock: int | None = None,
    ) -> None: ...

_REGISTRY: dict[str, type["Receiver"]] = {}
_UNAVAILABLE: dict[str, str] = {}


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


def unavailable_receivers() -> dict[str, str]:
    """Receiver types that failed to import, mapped to the reason."""
    return dict(_UNAVAILABLE)


def create_receiver(method: str, name: str, emit: Emit, arguments: dict) -> Receiver:
    """Instantiate the receiver registered under ``method``."""
    if method in _UNAVAILABLE:
        raise KeyError(
            f"receiver {method!r} is unavailable ({_UNAVAILABLE[method]}). "
            "Install the optional extra, e.g. pip install 'pyaerial[sdr]'."
        )
    if method not in _REGISTRY:
        raise KeyError(
            f"unknown receiver method {method!r}; available: {available_receivers()}"
        )
    return _REGISTRY[method](name, emit, arguments)


def register_builtins() -> None:
    """Import built-in receivers so they register themselves."""
    from pyaerial.receivers import dump1090 as _dump1090  # noqa: F401
    from pyaerial.receivers import replay as _replay  # noqa: F401

    try:  # pyrtlsdr / librtlsdr may be unavailable on some systems.
        from pyaerial.receivers import py1090 as _py1090  # noqa: F401
    except Exception as exc:  # pragma: no cover - optional dependency
        _UNAVAILABLE["py1090"] = str(exc)
        logging.getLogger("pyaerial.receiver").warning(
            "py1090 receiver unavailable (%s). Install with: pip install 'pyaerial[sdr]'",
            exc,
        )


register_builtins()
