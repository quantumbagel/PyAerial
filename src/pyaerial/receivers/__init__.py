"""Receiver plugin contract and the receiver registry."""

from __future__ import annotations

import abc
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, NamedTuple, Protocol

from pyaerial.plugins import PluginRegistry
from pyaerial.receivers.frames import RawFrame

__all__ = [
    "Emit",
    "RawFrame",
    "Receiver",
    "ReceiverEndpoint",
    "available_receivers",
    "create_receiver",
    "receiver_class",
    "receiver_sources",
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
        bearing: float | None = None,
        elevation: float | None = None,
        cfo: float | None = None,
    ) -> None: ...


class ReceiverEndpoint(NamedTuple):
    """Where a network receiver connects, and the wire format it expects there."""

    host: str
    port: int
    wire_format: str


class Receiver(abc.ABC):
    """Base class for all receivers.

    Plugin contract (see ARCHITECTURE.md): declare ``known_options``, implement
    :meth:`run`, and override :meth:`validate_options` / :meth:`endpoint` when
    they apply. Register with ``@register_receiver(name)`` or, for a separate
    distribution, a ``pyaerial.receivers`` entry point.
    """

    #: Every ``options`` key this receiver reads; ``validate`` rejects others.
    known_options: ClassVar[frozenset[str]]

    def __init__(self, name: str, emit: Emit, arguments: dict):
        self.name = name
        self.emit = emit
        self.arguments = arguments
        self.log = logging.getLogger(f"pyaerial.receiver.{name}")
        self._stop = threading.Event()
        self.configure(arguments)

    @classmethod
    def validate_options(cls, options: dict[str, Any], *, config_dir: Path) -> list[str]:
        """Problems with ``options`` as human-readable messages (empty: fine).

        Called by the config loader with the arguments the receiver will get.
        Must do no I/O beyond local file checks. May normalize values in place
        (e.g. resolve a path against ``config_dir``). A message starting with
        ``options.<key>:`` is reported against that key.
        """
        return []

    @classmethod
    def endpoint(cls, options: dict[str, Any]) -> ReceiverEndpoint | None:
        """Where this receiver connects, for receivers that read a Beast/AVR feed."""
        return None

    def configure(self, arguments: dict) -> None:  # noqa: B027 - optional hook
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


RECEIVERS: PluginRegistry[Receiver] = PluginRegistry(
    "receiver",
    "pyaerial.receivers",
    Receiver,
    {
        "aoa": "pyaerial.receivers.aoa:AoAReceiver",
        "dump1090": "pyaerial.receivers.dump1090:Dump1090Receiver",
        "readsb": "pyaerial.receivers.readsb:ReadsbReceiver",
        "replay": "pyaerial.receivers.replay:ReplayReceiver",
    },
)


def register_receiver(name: str) -> Callable[[type[Receiver]], type[Receiver]]:
    """Class decorator that registers a receiver under ``name``."""
    return RECEIVERS.decorator(name)


def available_receivers() -> list[str]:
    return RECEIVERS.names()


def receiver_class(name: str) -> type[Receiver] | None:
    return RECEIVERS.get(name)


def receiver_sources() -> dict[str, str]:
    """Receiver type -> ``"builtin"`` or the plugin's distribution name."""
    return RECEIVERS.sources()


def unavailable_receivers() -> dict[str, str]:
    """Receiver types that failed to load, mapped to the reason."""
    return RECEIVERS.unavailable_plugins()


def create_receiver(method: str, name: str, emit: Emit, arguments: dict) -> Receiver:
    """Instantiate the receiver registered under ``method``."""
    unavailable = RECEIVERS.unavailable_plugins()
    cls = RECEIVERS.get(method)
    if cls is None:
        if method in unavailable:
            raise KeyError(f"receiver {method!r} is unavailable ({unavailable[method]}).")
        raise KeyError(
            f"unknown receiver method {method!r}; available: {available_receivers()}"
        )
    return cls(name, emit, arguments)


def register_builtins() -> None:
    """Make sure the built-in (and discoverable) receivers are registered."""
    RECEIVERS.ensure_loaded()
