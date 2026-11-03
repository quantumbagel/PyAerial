"""
ADS-B receiver network: integrity-checked tracking, a labelled research archive, and zone alerts.

(c) 2024 Julian Reder (quantumbagel)
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the one place the version is written down.
    __version__ = version("pyaerial")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0+unknown"
