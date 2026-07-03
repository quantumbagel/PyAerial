"""Typed, validated configuration for PyAerial."""
from pyaerial.config.loader import ConfigError, load_config
from pyaerial.config.schema import Config

__all__ = ["Config", "load_config", "ConfigError"]
