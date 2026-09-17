from __future__ import annotations

import json
import math

from pyaerial.jsonutil import dumps, sanitize


def test_sanitize_replaces_non_finite_floats():
    data = {"eta": math.inf, "nan": float("nan"), "ok": 1.5, "nested": [-math.inf]}
    cleaned = sanitize(data)
    assert cleaned["eta"] is None
    assert cleaned["nan"] is None
    assert cleaned["ok"] == 1.5
    assert cleaned["nested"] == [None]


def test_dumps_never_emits_infinity():
    encoded = dumps({"eta": math.inf, "reason": {"zones": {"pad": math.inf}}})
    parsed = json.loads(encoded)
    assert parsed["eta"] is None
    assert parsed["reason"]["zones"]["pad"] is None
