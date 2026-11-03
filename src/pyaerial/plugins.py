"""Plugin registries: discovery through entry points, builtins first.

Receivers and alerters each keep one :class:`PluginRegistry`. This module
depends on nothing else in PyAerial, so both can use it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points
from typing import Generic, TypeVar

__all__ = ["BUILTIN", "PluginRegistry"]

log = logging.getLogger("pyaerial.plugins")

#: Source label for plugins shipped with PyAerial.
BUILTIN = "builtin"
_OWN_DISTRIBUTION = "pyaerial"


_P = TypeVar("_P")


def _distribution(entry: EntryPoint) -> str:
    dist = getattr(entry, "dist", None)
    name = getattr(dist, "name", None) or "unknown distribution"
    return str(name)


def _is_own(entry: EntryPoint) -> bool:
    return _distribution(entry).lower().replace("_", "-") == _OWN_DISTRIBUTION


class PluginRegistry(Generic[_P]):
    """Name -> plugin class for one entry-point group, discovered on first use.

    Builtins come from PyAerial's own entry points, or straight from
    ``builtins`` when the package metadata is missing (an editable checkout
    without it). Other distributions' entry points add third-party plugins.
    A builtin name cannot be taken over (warning). A plugin that fails to load,
    or does not declare ``known_options``, is logged once and listed in
    :attr:`unavailable`; it never stops ``validate`` or ``run``.

    Used by :mod:`pyaerial.receivers` and :mod:`pyaerial.alerters`.
    """

    def __init__(
        self, kind: str, group: str, base: type, builtins: dict[str, str]
    ) -> None:
        self.kind = kind
        self.group = group
        self.base = base
        self._builtins = builtins
        self._classes: dict[str, type[_P]] = {}
        self._sources: dict[str, str] = {}
        self.unavailable: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.RLock()

    # -- registration ---------------------------------------------------------

    def add(self, name: str, cls: type[_P], source: str) -> bool:
        """Register ``cls`` under ``name``; False if refused (logged)."""
        if not (isinstance(cls, type) and issubclass(cls, self.base)):
            self._refuse(name, source, f"{cls!r} is not a {self.base.__name__} subclass")
            return False
        if not isinstance(getattr(cls, "known_options", None), frozenset):
            if source != BUILTIN and not source.startswith("module "):
                self._refuse(name, source, "it does not declare known_options")
                return False
            # In-process registration (embedding code, tests): accept no
            # options, so anything configured for it is reported as unknown.
            log.warning(
                "%s %r (%s) does not declare known_options; it accepts no options",
                self.kind,
                name,
                cls.__qualname__,
            )
            cls.known_options = frozenset()  # type: ignore[attr-defined]
        current = self._classes.get(name)
        if current is not None and current is not cls:
            if self._sources.get(name) == BUILTIN and source != BUILTIN:
                log.warning(
                    "%s plugin %r from %s ignored: a builtin %s has that name",
                    self.kind,
                    name,
                    source,
                    self.kind,
                )
                return False
            log.warning(
                "%s %r from %s replaces the one from %s",
                self.kind,
                name,
                source,
                self._sources.get(name),
            )
        self._classes[name] = cls
        self._sources[name] = source
        self.unavailable.pop(name, None)
        return True

    def _refuse(self, name: str, source: str, reason: str) -> None:
        log.error("%s plugin %r from %s skipped: %s", self.kind, name, source, reason)
        self.unavailable.setdefault(name, f"{reason}; from {source}")

    def decorator(self, name: str) -> Callable[[type[_P]], type[_P]]:
        def register(cls: type[_P]) -> type[_P]:
            module = getattr(cls, "__module__", "")
            source = BUILTIN if module.startswith("pyaerial.") else f"module {module}"
            self.add(name, cls, source)
            return cls

        return register

    # -- discovery ------------------------------------------------------------

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return
            # Set first: a plugin module importing this package must not recurse.
            self._loaded = True
            try:
                found = list(entry_points(group=self.group))
            except Exception:  # noqa: BLE001 - broken metadata must not stop us
                log.exception("Could not read %s entry points", self.group)
                found = []
            for entry in found:
                if _is_own(entry):
                    self._load(entry, BUILTIN)
            # Fallback when PyAerial's own metadata is missing or stale.
            for name, target in self._builtins.items():
                if name not in self._classes:
                    module, _, attr = target.partition(":")
                    self.add(name, getattr(import_module(module), attr), BUILTIN)
            for entry in found:
                if not _is_own(entry):
                    self._load(entry, _distribution(entry))

    def _load(self, entry: EntryPoint, source: str) -> None:
        try:
            cls = entry.load()
        except Exception as exc:  # noqa: BLE001 - any plugin bug is contained
            log.error(
                "%s plugin %r from %s failed to load: %s: %s",
                self.kind,
                entry.name,
                source,
                type(exc).__name__,
                exc,
            )
            self.unavailable[entry.name] = (
                f"failed to load from {source}: {type(exc).__name__}: {exc}"
            )
            return
        self.add(entry.name, cls, source)

    # -- lookups --------------------------------------------------------------

    def get(self, name: str) -> type[_P] | None:
        self.ensure_loaded()
        return self._classes.get(name)

    def names(self) -> list[str]:
        self.ensure_loaded()
        return sorted(self._classes)

    def sources(self) -> dict[str, str]:
        """``name -> "builtin"`` or the providing distribution's name."""
        self.ensure_loaded()
        return {name: self._sources[name] for name in sorted(self._classes)}

    def unavailable_plugins(self) -> dict[str, str]:
        self.ensure_loaded()
        return dict(self.unavailable)
