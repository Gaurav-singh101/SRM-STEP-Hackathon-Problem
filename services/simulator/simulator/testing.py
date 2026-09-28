"""Test helpers for the simulator.

Shipped inside the package rather than duplicated across test modules: the
processor's and the API's tests both need generated telemetry to work against, and
CLAUDE.md S7 puts a helper used in more than one place into the library.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import numpy as np

from simulator.fleet import build_fleet
from simulator.journey import JourneyConfig, generate_day

#: Fixed simulated start instant. Chosen rather than "now", so a failing test
#: reproduces exactly (CLAUDE.md S12).
DAY_START: Final[datetime] = datetime(2026, 9, 29, 5, 0, 0, tzinfo=UTC)


def generate_for(
    *,
    seed: int,
    vehicles: int = 1,
    config: JourneyConfig | None = None,
    day_start: datetime | None = None,
) -> list[dict[str, Any]]:
    """Generate one day of telemetry for ``vehicles`` vehicles, as wire dicts.

    Deterministic in ``seed``: both the roster and the journeys derive from it.
    """
    roster = build_fleet(vehicle_count=vehicles, seed=seed)
    rng = np.random.default_rng(seed)
    start = day_start or DAY_START

    events: list[dict[str, Any]] = []
    for vehicle in roster:
        generated, _ = generate_day(vehicle, start, rng=rng, config=config)
        events.extend(generated)
    return events
