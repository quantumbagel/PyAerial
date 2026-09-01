from __future__ import annotations

from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.alerts.retain import should_retain
from pyaerial.models import Datum
from pyaerial.store.mongo import MongoStore
from helpers import make_config, make_rule
from pyaerial.config.schema import ZoneConfig


def _plane():
    return {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.725, 1.0)],
            STORE_LONG: [Datum(-78.695, 1.0)],
        },
        STORE_CALC_DATA: {
            STORE_HEADING: [Datum(0.0, 1.0)],
            STORE_HORIZ_SPEED: [Datum(0.0, 1.0)],
        },
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 1.0,
            STORE_MOST_RECENT_PACKET: 10.0,
        },
    }


def test_retain_false_is_honored_even_with_alerts():
    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[make_rule(name="warn", retain=False, dwell_seconds=1)],
            )
        }
    )
    store = MongoStore(config, {}, disabled=True)
    alerts = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 100.0,
        }
    ]
    assert should_retain(_plane(), alerts, config, store.polygons) is False


def test_retain_true_requires_dwell():
    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[make_rule(name="warn", retain=True, dwell_seconds=60)],
            )
        }
    )
    store = MongoStore(config, {}, disabled=True)
    short = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 10.0,
        }
    ]
    long = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 80.0,
        }
    ]
    assert should_retain(_plane(), short, config, store.polygons) is False
    assert should_retain(_plane(), long, config, store.polygons) is True


def test_disabled_store_finalize_succeeds_without_mongo():
    store = MongoStore(make_config(), {}, disabled=True)
    assert store.finalize_plane(_plane(), alerts=[]) is True
