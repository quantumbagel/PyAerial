"""JSON helpers that never emit NaN or Infinity."""

from __future__ import annotations

import json
import math
from typing import Any


def sanitize(data: Any) -> Any:
    """Replace non-finite floats with None so the result is strict JSON."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {key: sanitize(value) for key, value in data.items()}
    if isinstance(data, list):
        return [sanitize(value) for value in data]
    if isinstance(data, tuple):
        return [sanitize(value) for value in data]
    return data


def dumps(data: Any, **kwargs: Any) -> str:
    """``json.dumps`` after :func:`sanitize`, with ``allow_nan=False``."""
    kwargs.setdefault("separators", (",", ":"))
    kwargs.setdefault("allow_nan", False)
    return json.dumps(sanitize(data), **kwargs)
