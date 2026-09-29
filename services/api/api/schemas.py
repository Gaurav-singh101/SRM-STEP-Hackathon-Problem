"""API response models.

Separate from the repository dataclasses on purpose. The repository returns what the
database holds; these describe what the API promises. Keeping them apart means a column
rename is not an API break, and it keeps `Decimal` from being serialised as a float
somewhere along the way.

Money is serialised as a **string**, not a number. JSON numbers are IEEE 754 doubles in
every JavaScript client, so `3733.50` would arrive as a float and accumulate error the
moment the UI summed anything. A string round-trips exactly (D11).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from api.adapters.repository import (
    FleetSummary,
    IdleEventRow,
    LeaderboardRow,
    VehicleDetail,
)

Money = Annotated[Decimal, Field(description="Exact decimal amount, serialised as a string")]


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Assumptions(_Base):
    """Everything needed to reproduce a cost figure by hand.

    Returned beside every monetary value rather than documented elsewhere. CLAUDE.md S14
    requires it, and the reason is practical: a finance manager who cannot check a
    number will not act on it.
    """

    formula_version: str
    burn_rate_lph: float | None
    fuel_price_per_litre: Money | None = None
    price_id: str | None
    idle_threshold_seconds: int
    currency: str | None

    @field_serializer("fuel_price_per_litre")
    def _money(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


class IdleEvent(_Base):
    """One idle event."""

    idle_event_id: int
    vin: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    duration_minutes: float
    sample_count: int
    #: How much unobserved time this segment bridged. Surfaced so a figure resting on
    #: sparse samples is visibly weaker than one resting on dense samples (D1).
    max_gap_seconds: float
    excessive: bool
    pto_active_any: bool
    start_lat: float | None
    start_lon: float | None
    #: True when coordinates were coarsened for this caller's role (CLAUDE.md S11).
    location_coarsened: bool

    fuel_litres_est: float | None
    cost_est: Money | None
    #: Device-reported cross-check, when available (D2).
    fuel_litres_measured: float | None
    #: True for a BEV: there is no fuel cost, which is different from a zero cost (D9).
    bev_excluded: bool
    #: Always labelled an estimate, never a measurement.
    is_estimate: bool = True
    assumptions: Assumptions

    @field_serializer("cost_est")
    def _money(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    @classmethod
    def from_row(cls, row: IdleEventRow, *, location_coarsened: bool) -> Self:
        return cls(
            idle_event_id=row.idle_event_id,
            vin=row.vin,
            started_at=row.started_at,
            ended_at=row.ended_at,
            duration_seconds=row.duration_seconds,
            duration_minutes=round(row.duration_seconds / 60.0, 1),
            sample_count=row.sample_count,
            max_gap_seconds=row.max_gap_seconds,
            excessive=row.excessive,
            pto_active_any=row.pto_active_any,
            start_lat=row.start_lat,
            start_lon=row.start_lon,
            location_coarsened=location_coarsened,
            fuel_litres_est=row.fuel_litres_est,
            cost_est=row.cost_est,
            fuel_litres_measured=row.fuel_litres_measured,
            bev_excluded=row.bev_excluded,
            assumptions=Assumptions(
                formula_version=row.formula_version,
                burn_rate_lph=row.burn_rate_lph,
                price_id=row.price_id,
                idle_threshold_seconds=row.idle_threshold_seconds,
                currency=row.currency,
            ),
        )


class PageMeta(_Base):
    """Keyset pagination envelope.

    No total count, deliberately. An exact ``COUNT(*)`` over a partitioned table of
    billions of rows is its own expensive scan and would be stale immediately. A client
    follows ``next_cursor`` until it is null.
    """

    next_cursor: str | None
    page_size: int
    returned: int
    has_more: bool


class IdleEventPage(_Base):
    items: list[IdleEvent]
    page: PageMeta


class FleetSummaryResponse(_Base):
    """Fleet KPIs for the overview screen."""

    tenant_id: str
    window_from: datetime
    window_to: datetime
    vehicle_count: int
    vehicles_with_idle: int
    idle_event_count: int
    excessive_count: int
    total_idle_hours: float
    total_fuel_litres: float
    total_cost: Money
    currency: str
    open_alert_count: int

    #: Number of BEVs whose idle produced no fuel cost. Non-zero means the cost total
    #: covers fewer vehicles than `vehicles_with_idle`, and the UI must say so (D9).
    excluded_bev_count: int
    cost_total_is_partial: bool

    is_estimate: bool = True
    assumptions: Assumptions

    @field_serializer("total_cost")
    def _money(self, value: Decimal) -> str:
        return str(value)

    @classmethod
    def from_summary(cls, summary: FleetSummary) -> Self:
        return cls(
            tenant_id=summary.tenant_id,
            window_from=summary.window_from,
            window_to=summary.window_to,
            vehicle_count=summary.vehicle_count,
            vehicles_with_idle=summary.vehicles_with_idle,
            idle_event_count=summary.idle_event_count,
            excessive_count=summary.excessive_count,
            total_idle_hours=round(summary.total_idle_hours, 2),
            total_fuel_litres=round(summary.total_fuel_litres, 2),
            total_cost=summary.total_cost,
            currency=summary.currency,
            open_alert_count=summary.open_alert_count,
            excluded_bev_count=summary.excluded_bev_count,
            cost_total_is_partial=summary.excluded_bev_count > 0,
            assumptions=Assumptions(
                formula_version=summary.formula_version,
                burn_rate_lph=None,
                price_id=None,
                idle_threshold_seconds=summary.idle_threshold_seconds,
                currency=summary.currency,
            ),
        )


class LeaderboardEntry(_Base):
    """One vehicle on the idle-cost leaderboard."""

    rank: int
    vin: str
    registration: str
    vehicle_class: str
    powertrain: str
    fleet_name: str
    idle_event_count: int
    excessive_count: int
    total_idle_hours: float
    total_fuel_litres: float
    total_cost: Money
    currency: str

    @field_serializer("total_cost")
    def _money(self, value: Decimal) -> str:
        return str(value)

    @classmethod
    def from_row(cls, row: LeaderboardRow, rank: int) -> Self:
        return cls(
            rank=rank,
            vin=row.vin,
            registration=row.registration,
            vehicle_class=row.vehicle_class,
            powertrain=row.powertrain,
            fleet_name=row.fleet_name,
            idle_event_count=row.idle_event_count,
            excessive_count=row.excessive_count,
            total_idle_hours=round(row.total_idle_hours, 2),
            total_fuel_litres=round(row.total_fuel_litres, 2),
            total_cost=row.total_cost,
            currency=row.currency,
        )


class LeaderboardResponse(_Base):
    items: list[LeaderboardEntry]
    since: datetime
    #: Stated explicitly: BEVs cannot appear because they have no fuel cost to rank by.
    excludes_bev: bool = True
    is_estimate: bool = True


class VehicleResponse(_Base):
    """Vehicle master data plus idle totals."""

    vin: str
    registration: str
    tenant_id: str
    fleet_name: str
    vehicle_class: str
    powertrain: str
    fuel_type: str | None
    model_year: int
    is_active: bool
    #: False for a BEV. When false, `total_cost` is null by design, not by omission.
    burns_fuel: bool
    idle_event_count: int
    total_idle_hours: float
    total_cost: Money | None
    currency: str | None

    @field_serializer("total_cost")
    def _money(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    @classmethod
    def from_detail(cls, detail: VehicleDetail) -> Self:
        return cls(
            vin=detail.vin,
            registration=detail.registration,
            tenant_id=detail.tenant_id,
            fleet_name=detail.fleet_name,
            vehicle_class=detail.vehicle_class,
            powertrain=detail.powertrain,
            fuel_type=detail.fuel_type,
            model_year=detail.model_year,
            is_active=detail.is_active,
            burns_fuel=detail.burns_fuel,
            idle_event_count=detail.idle_event_count,
            total_idle_hours=round(detail.total_idle_hours, 2),
            total_cost=detail.total_cost,
            currency=detail.currency,
        )


class TrendPoint(_Base):
    day: date
    idle_hours: float
    cost: Money

    @field_serializer("cost")
    def _money(self, value: Decimal) -> str:
        return str(value)


class TrendResponse(_Base):
    items: list[TrendPoint]
    currency: str
    is_estimate: bool = True


class HealthResponse(_Base):
    status: str
    service: str
    version: str


class ReadyResponse(_Base):
    """Readiness, with per-dependency detail.

    ``degraded`` rather than a flat failure when a non-essential store is down: the API
    serves partial data with a flag instead of returning a wall of 500s
    (CLAUDE.md S10 graceful degradation).
    """

    status: str
    postgres: bool
    redis: bool
    degraded: bool
