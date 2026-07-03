"""
Parsing, validation, and application of packet save-method strings.

Supported methods:

* ``all``            - keep every datum
* ``none``           - keep nothing
* ``decimate(n)``    - keep every ``n``-th datum
* ``sdecimate(x,y)`` - keep at most ``x`` data per ``y`` second window
"""
from __future__ import annotations

from dataclasses import dataclass

from pyaerial.constants import (
    CONFIG_CAT_SAVE_METHOD_ALL,
    CONFIG_CAT_SAVE_METHOD_ARGS,
    CONFIG_CAT_SAVE_METHOD_DECIMATE,
    CONFIG_CAT_SAVE_METHOD_NONE,
    CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE,
)
from pyaerial.models import Datum


class SaveMethodError(ValueError):
    """Raised when a save-method string is invalid."""


@dataclass(frozen=True)
class SaveMethod:
    name: str
    args: tuple[float, ...] = ()


def parse_save_method(method: str) -> SaveMethod:
    """Parse and validate a save-method string into a :class:`SaveMethod`."""
    method = method.strip()
    if "(" not in method:
        name = method
        if name not in CONFIG_CAT_SAVE_METHOD_ARGS:
            raise SaveMethodError(f"unknown save method {method!r}")
        if CONFIG_CAT_SAVE_METHOD_ARGS[name] != 0:
            raise SaveMethodError(
                f"save method {name!r} requires {CONFIG_CAT_SAVE_METHOD_ARGS[name]} argument(s)"
            )
        return SaveMethod(name)

    if not method.endswith(")") or method.count("(") != 1 or method.count(")") != 1:
        raise SaveMethodError(f"malformed save method {method!r} (check parentheses)")

    name, _, raw_args = method[:-1].partition("(")
    name = name.strip()
    if name not in CONFIG_CAT_SAVE_METHOD_ARGS:
        raise SaveMethodError(f"unknown save method {name!r}")

    expected = CONFIG_CAT_SAVE_METHOD_ARGS[name]
    if expected == 0:
        raise SaveMethodError(f"save method {name!r} does not take arguments")

    parts = [p.strip() for p in raw_args.split(",")]
    if any(p == "" for p in parts):
        raise SaveMethodError(f"empty/trailing argument in save method {method!r}")
    try:
        args = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise SaveMethodError(f"non-numeric argument in save method {method!r}") from exc
    if len(args) != expected:
        raise SaveMethodError(
            f"save method {name!r} expects {expected} argument(s), got {len(args)}"
        )
    return SaveMethod(name, args)


def filter_packets(packets: list[Datum], method: str | SaveMethod) -> list[Datum]:
    """Filter a list of :class:`Datum` according to ``method``."""
    spec = method if isinstance(method, SaveMethod) else parse_save_method(method)

    if spec.name == CONFIG_CAT_SAVE_METHOD_ALL:
        return packets
    if spec.name == CONFIG_CAT_SAVE_METHOD_NONE:
        return []
    if spec.name == CONFIG_CAT_SAVE_METHOD_DECIMATE:
        step = int(spec.args[0]) or 1
        return [p for i, p in enumerate(packets) if i % step == 0]
    if spec.name == CONFIG_CAT_SAVE_METHOD_SMART_DECIMATE:
        if not packets:
            return []
        max_per_window, window = spec.args
        kept: list[Datum] = []
        window_end = packets[0].time + window
        count = 0
        for packet in packets:
            if packet.time >= window_end:
                window_end = packet.time + window
                count = 0
            if count < max_per_window:
                kept.append(packet)
                count += 1
        return kept
    raise SaveMethodError(f"unknown save method {spec.name!r}")
