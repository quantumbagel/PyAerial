"""
Alerter plugins: actions taken when a plane matches a zone rule.

Alerters are selected by a rule action ``method`` (``print``, ``webhook``).
Built-ins and third-party alerters are discovered through the
``pyaerial.alerters`` entry-point group (see ARCHITECTURE.md).
"""

from __future__ import annotations

import abc
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from pyaerial.plugins import PluginRegistry


class Alerter(abc.ABC):
    """Base class for all alerters. One instance is reused across alerts.

    Plugin contract: declare ``known_options``, implement :meth:`alert`, and
    override :meth:`validate_options` when options need more than a key check.
    """

    #: Every ``options`` key this alerter reads; ``validate`` rejects others.
    known_options: ClassVar[frozenset[str]]

    def __init__(self, arguments: dict):
        self.arguments = arguments
        self.log = logging.getLogger(f"pyaerial.alerter.{self.method}")
        self.configure(arguments)

    #: Registered name; set on registration.
    method: str = "base"

    @classmethod
    def validate_options(cls, options: dict[str, Any], *, config_dir: Path) -> list[str]:
        """Problems with ``options`` as human-readable messages (empty: fine).

        Must do no I/O beyond local file checks; may normalize values in place.
        Messages must not echo secrets (webhook URLs, tokens).
        """
        return []

    def configure(self, arguments: dict) -> None:  # noqa: B027 - optional hook
        """Validate/store alerter-specific arguments. Override as needed."""

    @abc.abstractmethod
    def alert(self, meta: dict, payload: dict) -> None:
        """Deliver a single alert."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release any held resources. Override as needed."""


class _AlerterRegistry(PluginRegistry[Alerter]):
    def add(self, name: str, cls: type[Alerter], source: str) -> bool:
        added = super().add(name, cls, source)
        if added:
            cls.method = name
        return added


ALERTERS = _AlerterRegistry(
    "alerter",
    "pyaerial.alerters",
    Alerter,
    {
        "print": "pyaerial.alerters.printer:PrintAlerter",
        "webhook": "pyaerial.alerters.webhook:WebhookAlerter",
    },
)


def register_alerter(name: str) -> Callable[[type[Alerter]], type[Alerter]]:
    """Class decorator that registers an alerter under ``name``."""
    return ALERTERS.decorator(name)


def available_alerters() -> list[str]:
    return ALERTERS.names()


def alerter_class(method: str) -> type[Alerter] | None:
    return ALERTERS.get(method)


def alerter_sources() -> dict[str, str]:
    """Alerter method -> ``"builtin"`` or the plugin's distribution name."""
    return ALERTERS.sources()


def unavailable_alerters() -> dict[str, str]:
    """Alerter methods that failed to load, mapped to the reason."""
    return ALERTERS.unavailable_plugins()


def create_alerter(method: str, arguments: dict) -> Alerter:
    cls = ALERTERS.get(method)
    if cls is None:
        raise KeyError(
            f"unknown alert method {method!r}; available: {available_alerters()}"
        )
    return cls(arguments)


def register_builtins() -> None:
    """Make sure the built-in (and discoverable) alerters are registered."""
    ALERTERS.ensure_loaded()
