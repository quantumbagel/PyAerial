"""
Alerter plugins: actions taken when a plane satisfies a zone level.

Alerters replace the old hardcoded ``if method == 'print' / elif 'kafka'`` block.
New alerters register with :func:`register_alerter` and are selectable via a
category's ``alert_method``.
"""
from __future__ import annotations

import abc
import logging
from typing import Callable

_REGISTRY: dict[str, type["Alerter"]] = {}


class Alerter(abc.ABC):
    """Base class for all alerters. One instance is reused across alerts."""

    def __init__(self, arguments: dict):
        self.arguments = arguments
        self.log = logging.getLogger(f"pyaerial.alerter.{self.method}")
        self.configure(arguments)

    #: Registered name; set by :func:`register_alerter`.
    method: str = "base"

    def configure(self, arguments: dict) -> None:
        """Validate/store alerter-specific arguments. Override as needed."""

    @abc.abstractmethod
    def alert(self, meta: dict, payload: dict) -> None:
        """Deliver a single alert."""

    def close(self) -> None:
        """Release any held resources. Override as needed."""


def register_alerter(name: str) -> Callable[[type[Alerter]], type[Alerter]]:
    def decorator(cls: type[Alerter]) -> type[Alerter]:
        cls.method = name
        _REGISTRY[name] = cls
        return cls
    return decorator


def available_alerters() -> list[str]:
    return sorted(_REGISTRY)


def create_alerter(method: str, arguments: dict) -> Alerter:
    if method not in _REGISTRY:
        raise KeyError(
            f"unknown alert method {method!r}; available: {available_alerters()}"
        )
    return _REGISTRY[method](arguments)


from pyaerial.alerters import kafka as _kafka  # noqa: E402,F401
from pyaerial.alerters import printer as _printer  # noqa: E402,F401
