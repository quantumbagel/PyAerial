from __future__ import annotations

import pytest

from helpers import make_config
from pyaerial.config.schema import Config


@pytest.fixture
def config() -> Config:
    return make_config()
