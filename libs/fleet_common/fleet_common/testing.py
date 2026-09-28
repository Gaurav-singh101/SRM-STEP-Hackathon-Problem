"""Deterministic test factories for telemetry events.

Shipped inside the library rather than duplicated in each service's tests: the
simulator, processor and API test suites all need to build events, and the
rule-of-three in CLAUDE.md S7 says shared helpers live in ``libs/``.

Everything here is explicit and seeded. No function reads the wall clock or uses
unseeded randomness, so a failing test reproduces exactly (CLAUDE.md S12).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from fleet_common.events import Ignition, TelemetryEvent
from fleet_common.vin import repair_check_digit

#: Fixed epoch for tests. Chosen rather than "now", so failures are reproducible.
T0: Final[datetime] = datetime(2026, 9, 29, 8, 0, 0, tzinfo=UTC)

TEST_TENANT: Final[str] = "tenant-test"

#: A structurally valid VIN (correct check digit) for use across test suites.
TEST_VIN: Final[str] = repair_check_digit("1HGCM82633A004352")

#: Deterministic UUID source: event ids must be stable across runs so that a
#: failure is reproducible, which rules out uuid4() here.
_EVENT_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("00000000-0000-0000-0000-00000000f1ee")


def deterministic_event_id(vin: str, seq: int, offset: float) -> str:
    """A stable event id derived from identity, so reruns produce the same value."""
    return str(uuid.uuid5(_EVENT_ID_NAMESPACE, f"{vin}:{seq}:{offset}"))


def make_event(
    *,
    offset_seconds: float = 0.0,
    speed_kph: float = 0.0,
    ignition: Ignition = Ignition.ON,
    seq: int = 0,
    vin: str = TEST_VIN,
    tenant_id: str = TEST_TENANT,
    odometer_km: float = 1000.0,
    fuel_rate_lph: float | None = None,
    pto_active: bool | None = None,
    lat: float | None = 12.9716,
    lon: float | None = 77.5946,
    ts: datetime | None = None,
) -> TelemetryEvent:
    """Build one telemetry event at ``T0 + offset_seconds``.

    Defaults describe a stationary, engine-on vehicle - the idle case, since that
    is what most tests are about.
    """
    return TelemetryEvent(
        event_id=deterministic_event_id(vin, seq, offset_seconds),
        tenant_id=tenant_id,
        vin=vin,
        seq=seq,
        ts=ts if ts is not None else T0 + timedelta(seconds=offset_seconds),
        speed_kph=speed_kph,
        odometer_km=odometer_km,
        ignition=ignition,
        fuel_rate_lph=fuel_rate_lph,
        pto_active=pto_active,
        lat=lat,
        lon=lon,
    )


def make_idle_run(
    *,
    start: float,
    duration: float,
    step: float = 10.0,
    speed_kph: float = 0.0,
    fuel_rate_lph: float | None = None,
    vin: str = TEST_VIN,
) -> list[TelemetryEvent]:
    """A contiguous run of stationary, engine-on samples every ``step`` seconds.

    Inclusive of both endpoints, so ``duration`` is the observed span.
    """
    count = int(duration // step) + 1
    return [
        make_event(
            offset_seconds=start + i * step,
            speed_kph=speed_kph,
            seq=i,
            fuel_rate_lph=fuel_rate_lph,
            vin=vin,
        )
        for i in range(count)
    ]
