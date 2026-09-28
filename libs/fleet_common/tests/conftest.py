"""Pytest fixtures. Factories live in fleet_common.testing so every service's
test suite shares one implementation (CLAUDE.md S7).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from fleet_common.testing import T0, TEST_VIN


@pytest.fixture
def t0() -> datetime:
    return T0


@pytest.fixture
def valid_vin() -> str:
    return TEST_VIN
