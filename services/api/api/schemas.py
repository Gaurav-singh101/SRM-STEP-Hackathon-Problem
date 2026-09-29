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
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from api.adapters.repository import (
    AlertRow,
    FleetSummary,
    IdleEventRow,
    LeaderboardRow,
    UtilisationRow,
    UtilisationWindowRow,
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


class UtilisationEntry(_Base):
    """One vehicle's utilisation score.

    The three components are returned alongside the score, not instead of it. A single
    opaque number does not survive a fleet manager asking "why is this 31?", and the
    components are what make the answer checkable.
    """

    vin: str
    registration: str
    vehicle_class: str
    powertrain: str
    fleet_name: str
    score: float
    band: str
    active_day_component: float
    moving_hours_component: float
    distance_component: float
    idle_share: float
    #: Gap between the highest and lowest component. Decides the `abnormal` band by
    #: measuring the vehicle against itself rather than against its peers.
    component_spread: float
    #: Plain-language explanation, present only for `abnormal`.
    abnormal_reason: str | None
    window_days: int
    #: Peers the baseline was built from. A score computed against 5 vehicles is far
    #: weaker than one computed against 500, so the UI can qualify it.
    baseline_sample_size: int
    formula_version: str

    @classmethod
    def from_row(cls, row: UtilisationRow) -> Self:
        return cls(
            vin=row.vin,
            registration=row.registration,
            vehicle_class=row.vehicle_class,
            powertrain=row.powertrain,
            fleet_name=row.fleet_name,
            score=round(row.score, 1),
            band=row.band,
            active_day_component=round(row.active_day_component, 3),
            moving_hours_component=round(row.moving_hours_component, 3),
            distance_component=round(row.distance_component, 3),
            idle_share=round(row.idle_share, 3),
            component_spread=round(row.component_spread, 3),
            abnormal_reason=row.abnormal_reason,
            window_days=row.window_days,
            baseline_sample_size=row.baseline_sample_size,
            formula_version=row.formula_version,
        )


class UtilisationCoverage(_Base):
    """How much of the fleet this window could actually score.

    Present so the page cannot imply every vehicle was scored. The two unscored counts
    are kept apart because they mean different things and lead to different actions:
    chase a silent telematics unit, or accept that a small class has no usable peer
    group.
    """

    window_end: date
    window_days: int
    vehicles_active: int
    scored: int
    #: No usable activity reached us. **Not** the same as unused: a failed telematics
    #: unit and a genuinely parked vehicle both send nothing, and scoring either as idle
    #: would tell a fleet manager to sell a van that works.
    unscored_no_telemetry: int
    #: In a vehicle class with fewer peers than a baseline needs, so skipped rather than
    #: scored against noise.
    unscored_class_too_small: int
    formula_version: str

    @classmethod
    def from_row(cls, row: UtilisationWindowRow) -> UtilisationCoverage:
        return cls(
            window_end=row.window_end,
            window_days=row.window_days,
            vehicles_active=row.vehicles_active,
            scored=row.scored,
            unscored_no_telemetry=row.unscored_no_telemetry,
            unscored_class_too_small=row.unscored_class_too_small,
            formula_version=row.formula_version,
        )


class UtilisationPage(_Base):
    items: list[UtilisationEntry]
    page: PageMeta
    #: ``None`` when no window has been scored yet, which is a different statement from
    #: "every vehicle was scored" and must not be rendered as zero.
    coverage: UtilisationCoverage | None = None


class AlertEntry(_Base):
    """One alert needing attention."""

    alert_id: int
    vin: str
    registration: str
    alert_type: str
    severity: str
    triggering_event_ts: datetime
    persisted_at: datetime
    #: The D10 SLO: from the event happening to the alert being durable. Meaningful for
    #: live streaming only - on replayed history it measures the age of the data.
    end_to_end_latency_seconds: float
    #: From the processor receiving the event to the alert being durable. Meaningful
    #: whatever the age of the data, so this is the figure to quote for a backfill.
    processing_latency_seconds: float | None
    #: True when this alert came from replayed history. Returned so a latency figure is
    #: never read without the context that decides whether it means anything.
    is_backfill: bool
    idle_event_started_at: datetime | None
    detail: dict[str, Any]
    acknowledged: bool

    @classmethod
    def from_row(cls, row: AlertRow) -> Self:
        return cls(
            alert_id=row.alert_id,
            vin=row.vin,
            registration=row.registration,
            alert_type=row.alert_type,
            severity=row.severity,
            triggering_event_ts=row.triggering_event_ts,
            persisted_at=row.persisted_at,
            end_to_end_latency_seconds=round(row.end_to_end_latency_seconds, 3),
            processing_latency_seconds=(
                round(row.processing_latency_seconds, 3)
                if row.processing_latency_seconds is not None
                else None
            ),
            is_backfill=row.is_backfill,
            idle_event_started_at=row.idle_event_started_at,
            detail=row.detail,
            acknowledged=row.acknowledged_at is not None,
        )


class AlertsResponse(_Base):
    items: list[AlertEntry]
    unacknowledged_only: bool
    #: Worst PROCESSING latency in this page, against the D10 target of under 5s. The
    #: processing figure is used rather than end-to-end because it means the same thing
    #: for live and replayed data.
    max_processing_latency_seconds: float | None
    #: True when any alert here came from replayed history, so the UI can say plainly
    #: that end-to-end latency reflects data age rather than system speed.
    contains_backfill: bool = False
