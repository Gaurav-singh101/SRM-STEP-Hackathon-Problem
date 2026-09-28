"""Canonical telemetry event model.

Generated to match ``schemas/telemetry.raw.v1.json``, which is the single source
of truth for the wire contract (CLAUDE.md S7). A contract test asserts this
model and that schema stay in step, so neither can drift silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fleet_common.vin import is_valid_vin

SCHEMA_VERSION: Final[int] = 1

# Bounds duplicated from the JSON Schema. The contract test in
# libs/fleet_common/tests/test_contract_schema.py fails if they diverge.
MAX_SPEED_KPH: Final[float] = 300.0
MAX_ODOMETER_KM: Final[float] = 5_000_000.0


class Ignition(StrEnum):
    """Engine power state. Authoritative input to idle detection (D1)."""

    OFF = "off"
    ACCESSORY = "accessory"
    ON = "on"
    CRANKING = "cranking"


class EngineState(StrEnum):
    """Device-reported engine state.

    A hint only: OEMs compute it inconsistently, so idle detection derives its
    own state from ignition + speed and keeps that logic testable (D7).
    """

    OFF = "off"
    IDLING = "idling"
    RUNNING = "running"


class DiscreteEvent(StrEnum):
    IGNITION_ON = "IGNITION_ON"
    IGNITION_OFF = "IGNITION_OFF"
    HARSH_BRAKE = "HARSH_BRAKE"
    HARSH_ACCEL = "HARSH_ACCEL"
    PTO_ON = "PTO_ON"
    PTO_OFF = "PTO_OFF"
    TRIP_START = "TRIP_START"
    TRIP_END = "TRIP_END"


class Powertrain(StrEnum):
    """Lives on the vehicle master in Postgres, never on the event (D7/D9)."""

    DIESEL = "diesel"
    PETROL = "petrol"
    HYBRID = "hybrid"
    BEV = "bev"

    @property
    def burns_fuel(self) -> bool:
        """BEVs have no idle fuel cost; they are tracked for utilisation only (D9)."""
        return self is not Powertrain.BEV


Latitude = Annotated[float, Field(ge=-90.0, le=90.0)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0)]


class TelemetryEvent(BaseModel):
    """One vehicle signal sample at one instant of event time.

    Immutable: the processor enriches by constructing new objects rather than
    mutating, so an event that reached the DLQ is exactly what arrived.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: int = Field(default=SCHEMA_VERSION)
    event_id: str
    tenant_id: str = Field(min_length=1, max_length=64)
    device_id: str | None = Field(default=None, max_length=64)
    vin: str
    seq: int = Field(ge=0)
    ts: datetime

    speed_kph: float = Field(ge=0.0, le=MAX_SPEED_KPH)
    odometer_km: float = Field(ge=0.0, le=MAX_ODOMETER_KM)
    heading_deg: float | None = Field(default=None, ge=0.0, lt=360.0)

    ignition: Ignition
    engine_state: EngineState | None = None
    rpm: int | None = Field(default=None, ge=0, le=10_000)
    engine_hours: float | None = Field(default=None, ge=0.0, le=200_000.0)

    fuel_level_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    fuel_rate_lph: float | None = Field(default=None, ge=0.0, le=200.0)
    soc_pct: float | None = Field(default=None, ge=0.0, le=100.0)

    lat: Latitude | None = None
    lon: Longitude | None = None
    gps_valid: bool | None = None
    hdop: float | None = Field(default=None, ge=0.0, le=50.0)

    dtc: tuple[str, ...] | None = None
    evt: DiscreteEvent | None = None
    pto_active: bool | None = None

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {v}, expected {SCHEMA_VERSION}")
        return v

    @field_validator("vin")
    @classmethod
    def _valid_vin(cls, v: str) -> str:
        """Reject malformed VINs at the edge so they reach the DLQ, not the sink."""
        if not is_valid_vin(v):
            raise ValueError(f"invalid VIN: {v!r}")
        return v

    @field_validator("ts")
    @classmethod
    def _tz_aware_utc(cls, v: datetime) -> datetime:
        """Event time must be unambiguous.

        A naive timestamp silently takes on the reader's timezone, which would
        corrupt every duration and every window boundary downstream.
        """
        if v.tzinfo is None:
            raise ValueError("ts must be timezone-aware (UTC)")
        return v.astimezone(UTC)

    @property
    def is_stationary(self) -> bool:
        """Speed alone; the idle threshold itself is config, never hard-coded (D1)."""
        return self.speed_kph <= 0.0

    @property
    def engine_powered(self) -> bool:
        """True when the engine can burn fuel. 'accessory' cannot, so it is not idle."""
        return self.ignition in (Ignition.ON, Ignition.CRANKING)
