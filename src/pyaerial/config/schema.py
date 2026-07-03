"""
Typed configuration schema (pydantic v2).

This replaces the old ad-hoc ``validator.py``: validation now happens by
constructing :class:`Config` from the parsed YAML, producing clear per-field
errors. The canonical save schema is nested::

    save:
      telemetry:
        default: all
      calculated:
        default: all

(The old ``telemetry_method``/``calculated_method`` flat keys are gone.)
"""
from __future__ import annotations

from typing import Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pyaerial import expr
from pyaerial.constants import (
    CONFIG_COMP_CTYPE_MAXIMUM,
    CONFIG_COMP_CTYPE_MINIMUM,
    LOGGING_LEVELS,
)
from pyaerial.save_methods import SaveMethodError, parse_save_method


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralConfig(_Strict):
    mongodb: str = "mongodb://localhost:27017"
    backdate_packets: int = Field(default=10, gt=0)
    remember_planes: float = Field(default=30, gt=0)
    status_message_top_planes: int = Field(default=5, ge=-1)
    advanced_status: bool = True
    hz: float = Field(default=2, gt=0)
    duplicate_packet_merging: float = Field(default=5, ge=0)
    logs: str = "info"
    log_file: str | None = None
    saver: str = "mongo"

    @field_validator("logs")
    @classmethod
    def _check_level(cls, value: str) -> str:
        if value not in LOGGING_LEVELS:
            raise ValueError(f"must be one of {sorted(LOGGING_LEVELS)}")
        return value


class HomeConfig(_Strict):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ReceiverConfig(_Strict):
    method: str
    arguments: dict[str, object] = Field(default_factory=dict)


class ComparisonSpec(_Strict):
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "ComparisonSpec":
        if self.minimum is None and self.maximum is None:
            raise ValueError("a component field must define 'minimum' and/or 'maximum'")
        return self

    def as_pairs(self) -> dict[str, float]:
        pairs: dict[str, float] = {}
        if self.minimum is not None:
            pairs[CONFIG_COMP_CTYPE_MINIMUM] = self.minimum
        if self.maximum is not None:
            pairs[CONFIG_COMP_CTYPE_MAXIMUM] = self.maximum
        return pairs


# A component is a mapping of data-field name -> comparison spec.
Component = dict[str, ComparisonSpec]


class CategorySave(_Strict):
    telemetry: dict[str, str] = Field(default_factory=lambda: {"default": "all"})
    calculated: dict[str, str] = Field(default_factory=lambda: {"default": "all"})

    @field_validator("telemetry", "calculated")
    @classmethod
    def _validate_methods(cls, value: dict[str, str]) -> dict[str, str]:
        for field, method in value.items():
            try:
                parse_save_method(method)
            except SaveMethodError as exc:
                raise ValueError(f"invalid save method for {field!r}: {exc}") from exc
        return value


class CategoryConfig(_Strict):
    alert_method: str = "print"
    arguments: dict[str, object] = Field(default_factory=dict)
    save: CategorySave = Field(default_factory=CategorySave)


class LevelConfig(_Strict):
    category: Union[str, CategoryConfig]
    requirements: str
    seconds: int = Field(gt=0)

    @field_validator("requirements")
    @classmethod
    def _validate_requirements(cls, value: str) -> str:
        # Ensure the expression parses safely; component existence is checked
        # later at the Config level once all components are known.
        expr.extract_component_names(value)
        return value


class ZoneConfig(_Strict):
    coordinates: list[list[float]] = Field(min_length=3)
    levels: dict[str, LevelConfig]

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinates(cls, value: list[list[float]]) -> list[list[float]]:
        for point in value:
            if len(point) != 2:
                raise ValueError("each coordinate must be a [latitude, longitude] pair")
        return value


class Config(_Strict):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    home: HomeConfig
    receivers: dict[str, ReceiverConfig]
    components: dict[str, Component] = Field(default_factory=dict)
    zones: dict[str, ZoneConfig] = Field(default_factory=dict)
    categories: dict[str, CategoryConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_reference(self) -> "Config":
        if not self.receivers:
            raise ValueError("at least one receiver must be configured")

        for zone_name, zone in self.zones.items():
            for level_name, level in zone.levels.items():
                where = f"zone {zone_name!r} level {level_name!r}"

                # Category reference must resolve.
                if isinstance(level.category, str) and level.category not in self.categories:
                    raise ValueError(f"{where} references unknown category {level.category!r}")

                # Every requirement component must exist.
                for component in expr.extract_component_names(level.requirements):
                    if component not in self.components:
                        raise ValueError(
                            f"{where} requirement references unknown component {component!r}"
                        )
        return self

    def resolve_category(self, category: Union[str, CategoryConfig]) -> CategoryConfig:
        """Return the concrete :class:`CategoryConfig` for a str/inline reference."""
        if isinstance(category, str):
            return self.categories[category]
        return category
