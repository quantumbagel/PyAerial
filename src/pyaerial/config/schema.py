"""Pydantic v2 configuration models for PyAerial tracking and infrastructure."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shapely import Polygon
from shapely.validation import explain_validity

from pyaerial.constants import LOGGING_LEVELS, WHEN_FIELDS, WHEN_SPATIAL_FIELDS

_HEX_COLOR_RE = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"


class _Strict(BaseModel):
    # inf/nan satisfy no useful bound: hz: .inf busy-loops, and a NaN rule
    # threshold silently never matches.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


_REDIS_SCHEMES = ("redis://", "rediss://", "unix://")


class DatabaseConfig(_Strict):
    path: str = "pyaerial.db"
    # repr=False: may carry a password (inline, or from redis_password_file).
    redis_uri: str = Field(default="redis://localhost:6379/0", repr=False)
    # File holding the Redis password (e.g. a Docker secret), so it needs not
    # be in redis_uri / PYAERIAL_REDIS. The loader adds it to redis_uri.
    redis_password_file: str | None = None

    @field_validator("redis_uri")
    @classmethod
    def _check_redis_uri(cls, value: str) -> str:
        # redis-py raises a bare ValueError for these when the store is built.
        if not value.strip().lower().startswith(_REDIS_SCHEMES):
            raise ValueError(
                "must start with redis://, rediss:// or unix:// "
                "(for example redis://localhost:6379/0)"
            )
        return value.strip()

    @model_validator(mode="before")
    @classmethod
    def _reject_mongo_keys(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        leftover = [key for key in ("uri", "name") if key in data]
        if leftover:
            labels = ", ".join(f"database.{key}" for key in leftover)
            raise ValueError(
                f"{labels} (MongoDB) are no longer used. "
                "Set database.path to a SQLite file (default pyaerial.db) "
                "and keep database.redis_uri for the live store."
            )
        return data


class TrackingConfig(_Strict):
    backdate_packets: int = Field(default=10, gt=0)
    remember_planes: float = Field(default=120, gt=0)
    status_message_top_planes: int = Field(default=5, ge=-1)
    advanced_status: bool = True
    hz: float = Field(default=2, gt=0)
    duplicate_packet_merging: float = Field(default=5, ge=0)
    use_kalman_eta: bool = False
    curved_projection: bool = False
    telemetry_keep_seconds: float = Field(default=600, ge=0)
    status_interval: float = Field(default=10, gt=0)
    # A flight tracked longer than this is archived in segments of at most
    # this length (a vehicle or balloon heard all day). 0 = no limit.
    max_flight_seconds: float = Field(default=14400.0, ge=0)


class ArchiveConfig(_Strict):
    # Which completed flights SQLite keeps: "all" archives every flight with at
    # least ``min_positions`` position samples (plus any flight a retain: true
    # rule alerted on); "rules" archives only flights a retain: true rule matched.
    flights: Literal["all", "rules"] = "all"
    min_positions: int = Field(default=2, ge=1)
    # Store every received frame, one row per receiver copy, in ``frames``.
    raw_frames: bool = False
    # Delete archived frames older than this many hours; 0 keeps them forever.
    raw_frames_keep_hours: float = Field(default=24, ge=0)


_LEVEL_ALIASES = {"warn": "warning"}


class LoggingConfig(_Strict):
    level: str = "info"
    file: str | None = None

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        value = value.strip().lower()
        value = _LEVEL_ALIASES.get(value, value)
        if value not in LOGGING_LEVELS:
            raise ValueError(f"must be one of {sorted(LOGGING_LEVELS)}")
        return value


class HomeConfig(_Strict):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


_DUMP1090_FORMATS = {"avr", "raw", "beast", "binary"}


class ReceiverPositionConfig(_Strict):
    """Antenna location. ``altitude`` is metres above mean sea level."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude: float = 0.0


class ReceiverConfig(_Strict):
    type: str
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    format: str | None = None
    # Fixed antenna location. When unset, dump1090 receivers poll
    # ``position_url`` (default http://<host>:8080/data/receiver.json) and fall
    # back to ``home``.
    position: ReceiverPositionConfig | None = None
    position_url: str | None = None
    # Antenna height above ground (m) for the radio-horizon check.
    antenna_height: float = Field(default=10.0, ge=0)
    options: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _dump1090_format(self) -> "ReceiverConfig":
        if self.type == "dump1090" and self.format is not None:
            fmt = str(self.format).lower()
            if fmt not in _DUMP1090_FORMATS:
                raise ValueError(
                    "dump1090 format must be one of avr, raw, beast, binary"
                )
            self.format = fmt
        return self

    def receiver_arguments(self) -> dict[str, object]:
        """Build the argument dict expected by receiver plugins."""
        args = dict(self.options)
        if self.type in ("dump1090", "aoa"):
            if self.host is not None:
                args.setdefault("tcp_connection_ip", self.host)
            if self.port is not None:
                args.setdefault("tcp_connection_port", self.port)
        if self.type == "dump1090":
            if self.format is not None:
                args.setdefault("format", self.format)
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
        from pyaerial.constants import (
            CONFIG_COMP_CTYPE_MAXIMUM,
            CONFIG_COMP_CTYPE_MINIMUM,
        )

        pairs: dict[str, float] = {}
        if self.minimum is not None:
            pairs[CONFIG_COMP_CTYPE_MINIMUM] = self.minimum
        if self.maximum is not None:
            pairs[CONFIG_COMP_CTYPE_MAXIMUM] = self.maximum
        return pairs


_CIRCULAR_WHEN_FIELDS = frozenset({"heading", "direction"})


class AlertActionConfig(_Strict):
    method: str
    options: dict[str, object] = Field(default_factory=dict)


class WhileActiveConfig(_Strict):
    interval_seconds: int = Field(default=60, gt=0)
    actions: list[AlertActionConfig] = Field(default_factory=list)


class RuleConfig(_Strict):
    name: str
    color: str | None = None
    when: dict[str, FieldConstraint] = Field(min_length=1)
    dwell_seconds: int = Field(gt=0)
    retain: bool = True
    hysteresis_seconds: float = Field(default=0, ge=0)
    predict_seconds: float | None = Field(default=None, ge=0)
    on_activate: list[AlertActionConfig] = Field(default_factory=list)
    on_deactivate: list[AlertActionConfig] = Field(default_factory=list)
    while_active: WhileActiveConfig | None = None

    def activation_hold_seconds(self) -> float:
        """Seconds `when` must hold before the rule activates.

        ``hysteresis_seconds`` wins when set so existing configs that tune the
        on-delay keep working. When it is left at 0, ``dwell_seconds`` is the
        hold. Otherwise, Discord/webhooks fire on every 0.5s flicker.
        """
        if self.hysteresis_seconds > 0:
            return float(self.hysteresis_seconds)
        return float(self.dwell_seconds)

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str | None) -> str | None:
        if value is not None and not re.match(_HEX_COLOR_RE, value):
            raise ValueError("color must be a hex value like #rrggbb")
        return value

    @field_validator("when")
    @classmethod
    def _validate_when_fields(
        cls, value: dict[str, FieldConstraint]
    ) -> dict[str, FieldConstraint]:
        unknown = sorted(name for name in value if name not in WHEN_FIELDS)
        if unknown:
            raise ValueError(
                f"unknown when field(s): {', '.join(unknown)}; "
                f"valid: {', '.join(sorted(WHEN_FIELDS))}"
            )
        if not any(name in WHEN_SPATIAL_FIELDS for name in value):
            raise ValueError(
                "when must include eta, distance, dist, or proximity "
                "(there is no implicit inside-polygon test)"
            )
        for name, constraint in value.items():
            # Heading windows wrap through north ({min: 350, max: 10}).
            if name in _CIRCULAR_WHEN_FIELDS:
                continue
            low, high = constraint.minimum, constraint.maximum
            if low is not None and high is not None and low > high:
                raise ValueError(
                    f"{name}: min ({low:g}) is greater than max ({high:g}); "
                    "the rule could never match"
                )
        return value


class ZoneConfig(_Strict):
    color: str | None = None
    coordinates: list[list[float]] | None = None
    file: str | None = None
    rules: list[RuleConfig] = Field(min_length=1)

    @field_validator("color")
    @classmethod
    def _validate_color(cls, value: str | None) -> str | None:
        if value is not None and not re.match(_HEX_COLOR_RE, value):
            raise ValueError("color must be a hex value like #rrggbb")
        return value

    @field_validator("coordinates")
    @classmethod
    def _validate_coordinates(
        cls, value: list[list[float]] | None
    ) -> list[list[float]] | None:
        if value is None:
            return value
        for point in value:
            if len(point) != 2:
                raise ValueError("each coordinate must be a [latitude, longitude] pair")
            lat, lon = point[0], point[1]
            if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
                raise ValueError(
                    "each coordinate must be [latitude, longitude] within "
                    "±90 / ±180"
                )
        # The closing point of a ring repeats the first; [a, b, a] is a line.
        if len({(point[0], point[1]) for point in value}) < 3:
            raise ValueError("coordinates must include at least 3 distinct points")
        polygon = Polygon(value)
        if polygon.convex_hull.area == 0:
            raise ValueError("polygon has zero area (all points lie on one line)")
        if not polygon.is_valid:
            raise ValueError(
                f"polygon is invalid ({explain_validity(polygon)}); "
                "edges must not self-intersect"
            )
        return value

    @field_validator("rules")
    @classmethod
    def _unique_rule_names(cls, value: list[RuleConfig]) -> list[RuleConfig]:
        # Alert state is keyed by (icao, zone, rule name); duplicates collide.
        seen: set[str] = set()
        for rule in value:
            if rule.name in seen:
                raise ValueError(f"duplicate rule name {rule.name!r} in this zone")
            seen.add(rule.name)
        return value

    @model_validator(mode="after")
    def _coordinates_or_file(self) -> "ZoneConfig":
        if self.coordinates is None and not self.file:
            raise ValueError("zone must define coordinates or file")
        return self


INTEGRITY_CHECKS = (
    "aoa",
    "tdoa",
    "mode_s_altitude",
    "radio_horizon",
    "duplicate_icao",
    "cross_network",
    "rssi_range",
    "rssi_trend",
    "kinematics",
    "timing",
    "replay",
    "emitter_cluster",
    "mode_s_corroboration",
    "message_mix",
    "appearance",
    "identity",
)

# How much one failing check moves the score toward "spoofed". Geometric checks
# that need extra hardware (AoA, TDOA) or that physics settles outright
# (horizon, Mode S altitude) weigh most; heuristics weigh least.
DEFAULT_INTEGRITY_WEIGHTS: dict[str, float] = {
    "aoa": 0.7,
    "tdoa": 0.7,
    "mode_s_altitude": 0.6,
    "radio_horizon": 0.6,
    "duplicate_icao": 0.5,
    "cross_network": 0.5,
    "rssi_range": 0.4,
    "rssi_trend": 0.4,
    "kinematics": 0.4,
    "timing": 0.4,
    "replay": 0.4,
    "emitter_cluster": 0.5,
    "mode_s_corroboration": 0.2,
    "message_mix": 0.2,
    "appearance": 0.2,
    "identity": 0.3,
}


class CrossNetworkConfig(_Strict):
    """Opt-in comparison against a public aggregator (sends ICAO addresses out)."""

    enabled: bool = False
    # ``{icao}`` is replaced with the lower-case hex address. The response must
    # be readsb-style JSON with an ``ac`` list carrying ``lat``/``lon``.
    url: str = "https://api.adsb.lol/v2/hex/{icao}"
    min_interval_seconds: float = Field(default=2.0, gt=0)
    recheck_seconds: float = Field(default=120.0, gt=0)
    max_distance_km: float = Field(default=10.0, gt=0)
    timeout_seconds: float = Field(default=5.0, gt=0)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        text = value.strip()
        if not text.lower().startswith(("https://", "http://")):
            raise ValueError("url must be an http(s):// URL")
        if "{icao}" not in text:
            # Otherwise every aircraft is compared with one position and fails.
            raise ValueError("url must contain {icao} (replaced with the hex address)")
        return text


class IntegrityConfig(_Strict):
    enabled: bool = True
    weights: dict[str, float] = Field(default_factory=dict)
    disabled_checks: list[str] = Field(default_factory=list)
    # Score at or above ``ok_score`` is ok; below ``spoofed_score`` is spoofed;
    # in between is suspect.
    ok_score: float = Field(default=0.8, ge=0, le=1)
    spoofed_score: float = Field(default=0.5, ge=0, le=1)
    # Seconds of per-aircraft evidence kept for the sliding-window checks.
    window_seconds: float = Field(default=120.0, gt=0)
    # Radio horizon is multiplied by this to allow for refraction and ducting.
    horizon_margin: float = Field(default=1.25, ge=1)
    max_range_km: float = Field(default=500.0, gt=0)
    # A received signal this many dB stronger than the station model predicts
    # for the claimed distance fails rssi_range.
    rssi_excess_db: float = Field(default=15.0, gt=0)
    # Median |measured - expected| bearing, degrees.
    aoa_tolerance_deg: float = Field(default=15.0, gt=0)
    # Median TDOA path-difference residual, metres.
    tdoa_tolerance_m: float = Field(default=3000.0, gt=0)
    mode_s_altitude_tolerance_m: float = Field(default=100.0, gt=0)
    cross_network: CrossNetworkConfig = Field(default_factory=CrossNetworkConfig)

    @field_validator("weights")
    @classmethod
    def _known_weights(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = sorted(name for name in value if name not in INTEGRITY_CHECKS)
        if unknown:
            raise ValueError(
                f"unknown integrity check(s): {', '.join(unknown)}; "
                f"valid: {', '.join(INTEGRITY_CHECKS)}"
            )
        for name, weight in value.items():
            if not 0.0 <= weight <= 1.0:
                raise ValueError(f"weight for {name} must be between 0 and 1")
        return value

    @field_validator("disabled_checks")
    @classmethod
    def _known_disabled(cls, value: list[str]) -> list[str]:
        unknown = sorted(name for name in value if name not in INTEGRITY_CHECKS)
        if unknown:
            raise ValueError(
                f"unknown integrity check(s): {', '.join(unknown)}; "
                f"valid: {', '.join(INTEGRITY_CHECKS)}"
            )
        return value

    @model_validator(mode="after")
    def _ordered_scores(self) -> "IntegrityConfig":
        if self.spoofed_score > self.ok_score:
            raise ValueError("integrity.spoofed_score must not exceed ok_score")
        return self

    def weight(self, check: str) -> float:
        if check in self.disabled_checks:
            return 0.0
        return self.weights.get(check, DEFAULT_INTEGRITY_WEIGHTS[check])


_ORIGIN_RE = re.compile(r"^https?://[^/?#@\s]+$", re.IGNORECASE)
# Host header names as Starlette's TrustedHostMiddleware compares them: no
# scheme and no port (it strips the port before matching).
_ALLOWED_HOST_RE = re.compile(
    r"^(\*|(\*\.)?[A-Za-z0-9_](?:[A-Za-z0-9_.\-]*[A-Za-z0-9_])?)$"
)


class WebConfig(_Strict):
    # Browser origins allowed to open /ws/live, /ws/raw and /ws/beast besides the
    # portal's own origin (Origin matching the Host header). Entries are exact
    # ``scheme://host[:port]`` strings; ``"*"`` allows any origin.
    origins: list[str] = Field(default_factory=list)
    # Also allow any http(s)://localhost / 127.0.0.1 / [::1] origin (dev servers).
    allow_localhost_origins: bool = False
    # Host header allowlist (DNS-rebinding guard). Empty accepts any Host, but
    # same-origin WebSockets then only trust IP-literal and loopback hosts.
    allowed_hosts: list[str] = Field(default_factory=list)
    # Serve FastAPI's /docs, /redoc and /openapi.json.
    enable_docs: bool = False
    beast_host: str = "127.0.0.1"
    # Not 30005: that is dump1090's Beast port, which the engine reads by
    # default; re-serving there would feed the engine its own output.
    beast_port: int | None = Field(default=30105, le=65535)
    beast_max_clients: int = Field(default=16, ge=1)
    beast_max_clients_per_ip: int = Field(default=4, ge=1)
    # /ws/live + /ws/raw + /ws/beast connection caps and per-connection request limits.
    ws_max_connections: int = Field(default=256, ge=1)
    ws_max_connections_per_ip: int = Field(default=32, ge=1)
    # Reverse proxies whose X-Forwarded-For is trusted for the client IP
    # (uvicorn forwarded_allow_ips). Empty trusts only 127.0.0.1.
    trusted_proxies: list[str] = Field(default_factory=list)
    ws_max_inflight: int = Field(default=2, ge=1, le=16)
    ws_rate_limit: float = Field(default=50.0, gt=0)
    ws_rate_burst: int = Field(default=100, ge=1)
    # Shared by all of one client IP's connections, on top of the above.
    ws_rate_limit_per_ip: float = Field(default=200.0, gt=0)
    ws_rate_burst_per_ip: int = Field(default=400, ge=1)
    ws_request_workers: int = Field(default=4, ge=1, le=64)

    @field_validator("origins")
    @classmethod
    def _validate_origins(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip().rstrip("/")
            if text != "*" and not _ORIGIN_RE.match(text):
                raise ValueError(
                    f"origin {item!r} must be '*' or scheme://host[:port] "
                    "(for example https://portal.example.com)"
                )
            cleaned.append(text)
        return cleaned

    @field_validator("beast_port")
    @classmethod
    def _validate_beast_port(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            # 0 would bind a random port that no feeder can be pointed at.
            raise ValueError("must be 1-65535; use null to disable Beast TCP")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_allowed_hosts(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if not _ALLOWED_HOST_RE.match(text):
                raise ValueError(
                    f"allowed_hosts entry {item!r} must be a bare hostname "
                    "such as tracker.example or *.lan (no scheme or port)"
                )
            cleaned.append(text)
        return cleaned

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        # uvicorn silently ignores entries it cannot parse as an IP or network.
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if text != "*":
                try:
                    # strict, as uvicorn parses it: 10.0.0.1/8 is not a network.
                    ipaddress.ip_network(text)
                except ValueError:
                    raise ValueError(
                        f"trusted_proxies entry {item!r} must be an IP address, "
                        "a CIDR network (10.0.0.0/8) or '*'"
                    ) from None
            cleaned.append(text)
        return cleaned


class EnrichmentConfig(_Strict):
    # Look up registration, type, operator and photos for each aircraft on
    # hexdb.io and Planespotters (sends ICAO addresses to both). false keeps
    # PyAerial offline; the local aircraft.db cache is still read.
    enabled: bool = True


class Config(_Strict):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    home: HomeConfig
    receivers: dict[str, ReceiverConfig] = Field(min_length=1)
    zones: dict[str, ZoneConfig] = Field(default_factory=dict)
    alert_colors: dict[str, str] = Field(default_factory=dict)
    web: WebConfig = Field(default_factory=WebConfig)
    integrity: IntegrityConfig = Field(default_factory=IntegrityConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)

    @field_validator("alert_colors")
    @classmethod
    def _validate_alert_colors(cls, value: dict[str, str]) -> dict[str, str]:
        for color in value.values():
            if not re.match(_HEX_COLOR_RE, color):
                raise ValueError("alert_colors values must be hex values like #rrggbb")
        return value
