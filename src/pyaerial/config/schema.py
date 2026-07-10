"""
Typed configuration schema (pydantic v2).

PyAerial v2 uses a flat, portal-oriented layout::

    database, tracking, logging, home, receivers, zones
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pyaerial.constants import LOGGING_LEVELS


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatabaseConfig(_Strict):
    uri: str = "mongodb://localhost:27017"
    name: str | None = None
    redis_uri: str = "redis://localhost:6379/0"


class TrackingConfig(_Strict):
    backdate_packets: int = Field(default=10, gt=0)
    remember_planes: float = Field(default=30, gt=0)
    status_message_top_planes: int = Field(default=5, ge=-1)
    advanced_status: bool = True
    hz: float = Field(default=2, gt=0)
    duplicate_packet_merging: float = Field(default=5, ge=0)


class LoggingConfig(_Strict):
    level: str = "info"
    file: str | None = None

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        if value not in LOGGING_LEVELS:
            raise ValueError(f"must be one of {sorted(LOGGING_LEVELS)}")
        return value


class HomeConfig(_Strict):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ReceiverConfig(_Strict):
    type: str
    host: str | None = None
    port: int | None = None
    options: dict[str, object] = Field(default_factory=dict)

    def receiver_arguments(self) -> dict[str, object]:
        """Build the argument dict expected by receiver plugins."""
        args = dict(self.options)
        if self.type == "dump1090":
            if self.host is not None:
                args.setdefault("tcp_connection_ip", self.host)
            if self.port is not None:
                args.setdefault("tcp_connection_port", self.port)
        return args


class FieldConstraint(_Strict):
    """Numeric constraint on a telemetry/calculated field."""

    model_config = ConfigDict(populate_by_name=True)

    minimum: float | None = Field(default=None, alias="min")
    maximum: float | None = Field(default=None, alias="max")

    @model_validator(mode="after")
    def _at_least_one(self) -> "FieldConstraint":
        if self.minimum is None and self.maximum is None:
            raise ValueError("a field constraint must define 'min' and/or 'max'")
        return self

    def as_pairs(self) -> dict[str, float]:
        from pyaerial.constants import CONFIG_COMP_CTYPE_MAXIMUM, CONFIG_COMP_CTYPE_MINIMUM

        pairs: dict[str, float] = {}
        if self.minimum is not None:
            pairs[CONFIG_COMP_CTYPE_MINIMUM] = self.minimum
        if self.maximum is not None:
            pairs[CONFIG_COMP_CTYPE_MAXIMUM] = self.maximum
        return pairs


class AlertConfig(_Strict):
    method: str = "print"
    options: dict[str, object] = Field(default_factory=dict)


class RuleConfig(_Strict):
    name: str
    when: dict[str, FieldConstraint] = Field(min_length=1)
    dwell_seconds: int = Field(gt=0)
    alert: AlertConfig = Field(default_factory=AlertConfig)
    retain: bool = True


class ZoneConfig(_Strict):
    coordinates: list[list[float]] = Field(min_length=3)
    rules: list[RuleConfig] = Field(min_length=1)

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinates(cls, value: list[list[float]]) -> list[list[float]]:
        for point in value:
            if len(point) != 2:
                raise ValueError("each coordinate must be a [latitude, longitude] pair")
        return value


class Config(_Strict):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    home: HomeConfig
    receivers: dict[str, ReceiverConfig]
    zones: dict[str, ZoneConfig] = Field(default_factory=dict)
