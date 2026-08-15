from __future__ import annotations

from pyaerial.config.schema import (
    Config,
    FieldConstraint,
    HomeConfig,
    ReceiverConfig,
    RuleConfig,
    TrackingConfig,
    ZoneConfig,
)


def make_rule(
    name: str = "warn",
    *,
    retain: bool = True,
    dwell_seconds: int = 60,
    **when_fields: dict,
) -> RuleConfig:
    when = {
        key: FieldConstraint.model_validate(spec) for key, spec in when_fields.items()
    }
    if not when:
        when = {"altitude": FieldConstraint(maximum=2000)}
    return RuleConfig(
        name=name,
        when=when,
        dwell_seconds=dwell_seconds,
        retain=retain,
    )


def make_config(
    *,
    zones: dict | None = None,
    tracking: TrackingConfig | None = None,
    receivers: dict | None = None,
) -> Config:
    if zones is None:
        zones = {
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[make_rule(altitude={"max": 2000}, eta={"max": 120})],
            )
        }
    return Config(
        home=HomeConfig(latitude=35.7275, longitude=-78.6959),
        receivers=receivers or {"mock": ReceiverConfig(type="mock")},
        zones=zones,
        tracking=tracking or TrackingConfig(),
    )
