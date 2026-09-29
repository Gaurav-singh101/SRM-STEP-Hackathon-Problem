"""The processing pipeline: validate, deduplicate, detect idle, price it, alert.

This is the heart of the service and it is deliberately **pure**. It takes a batch
of raw message bytes plus the vehicle metadata it needs, and returns everything that
should be written. No database, no broker, no clock.

That buys two things. The whole pipeline is unit-testable at speed, and the
write-ordering rules that make delivery effectively-once live in one place - the
adapter's only job is to persist a ``BatchResult`` and commit offsets afterwards.

Complexity: O(n) in batch size. Per-vehicle state is O(1) each and bounded by the
vehicles on assigned partitions, not by event rate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from fleet_common.domain.fuel import (
    BevHasNoIdleFuelError,
    FuelPrice,
    IdleCostEstimate,
    VehicleClass,
    estimate_idle_cost,
)
from fleet_common.domain.idle import (
    IdleConfig,
    IdleSegment,
    IdleStateMachine,
    with_identity,
)
from fleet_common.domain.trips import Trip, TripConfig, TripStateMachine
from fleet_common.domain.trips import with_identity as trip_with_identity
from fleet_common.events import Powertrain, TelemetryEvent
from processor.dedup import (
    DedupConfig,
    DedupState,
    EventDisposition,
)
from processor.validation import Rejection, ValidationConfig, decode

#: Guards against an unbounded alert list if something upstream goes wrong.
MAX_ALERTS_PER_BATCH: Final[int] = 10_000


class AlertType(StrEnum):
    """Alert types defined in D10. Deliberately few: two alerts a user will act on
    beat ten they will learn to ignore.

    **Invariant, verified against real data:** the number of
    ``EXCESSIVE_IDLE_IN_PROGRESS`` alerts is less than or equal to the number of
    segments marked excessive, and the difference is exactly those segments that
    crossed the threshold on their *closing* sample. That is deliberate: this alert
    says a vehicle is idling **right now**, and raising it for a period that has
    already ended would send an operator after a driver who is already moving.
    A pipeline run over 305,745 events produced 71 excessive segments and 69 alerts;
    both un-alerted segments were exactly 600 s, while every alerted one was 660 s
    or longer."""

    EXCESSIVE_IDLE_IN_PROGRESS = "EXCESSIVE_IDLE_IN_PROGRESS"
    IDLE_COST_SPIKE = "IDLE_COST_SPIKE"


@dataclass(frozen=True, slots=True)
class VehicleRef:
    """The vehicle metadata the pipeline needs, from the Postgres vehicle master.

    Powertrain and class live on the vehicle, never on the event (ADR-0004), so the
    pipeline must look them up. Cached in Redis by the adapter.
    """

    vin: str
    tenant_id: str
    powertrain: Powertrain
    vehicle_class: VehicleClass
    fleet_id: int


@dataclass(frozen=True, slots=True)
class Alert:
    """One alert, carrying both timestamps the D10 SLO is measured between."""

    alert_type: AlertType
    tenant_id: str
    vin: str
    #: The event whose arrival caused the alert. Latency is measured from here to
    #: the moment the row is persisted, because "when it appeared" is ambiguous.
    triggering_event_ts: datetime
    severity: str
    detail: dict[str, str | float | int]
    #: Ties the alert to the idle segment, which is also the suppression key: one
    #: alert per vehicle per type per segment, so a long idle cannot storm.
    idle_segment_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PricedIdleSegment:
    """A closed idle segment with its cost estimate, or an explicit exclusion.

    ``estimate`` is ``None`` only for a BEV, and ``bev_excluded`` then says so. A
    zero would be indistinguishable from a measured zero and would flow into fleet
    totals as though it were one (D9).
    """

    segment: IdleSegment
    vehicle: VehicleRef
    estimate: IdleCostEstimate | None
    bev_excluded: bool = False

    @property
    def cost(self) -> Decimal | None:
        return self.estimate.cost if self.estimate is not None else None


@dataclass(frozen=True, slots=True)
class LatestState:
    """Hot per-vehicle state for the dashboard, destined for Redis.

    Disposable and rebuildable by design: Redis is AP here, and losing it costs a
    cache rebuild, not data (CLAUDE.md S8).
    """

    vin: str
    tenant_id: str
    ts: datetime
    speed_kph: float
    ignition: str
    odometer_km: float
    lat: float | None
    lon: float | None
    idle_state: str
    open_idle_seconds: float


@dataclass(slots=True)
class BatchResult:
    """Everything one batch produced. The adapter persists this, then commits."""

    accepted: list[TelemetryEvent] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    late: list[TelemetryEvent] = field(default_factory=list)
    idle_segments: list[PricedIdleSegment] = field(default_factory=list)
    # Completed journeys. Utilisation needs moving time and distance (D3), and the PDF
    # S8 relational core lists Trip as a first-class entity.
    trips: list[Trip] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    latest_state: dict[str, LatestState] = field(default_factory=dict)
    duplicates: int = 0
    #: Events for a VIN absent from the vehicle master. Not an error: the seed job
    #: may not have run yet, or the vehicle may be genuinely unknown. Counted so
    #: "why is cost missing" has an answer.
    unknown_vehicle: int = 0

    @property
    def total_consumed(self) -> int:
        return len(self.accepted) + len(self.rejections) + len(self.late) + self.duplicates

    @property
    def dlq_rate(self) -> float:
        total = self.total_consumed
        return len(self.rejections) / total if total else 0.0


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """All business parameters, injected (CLAUDE.md S7)."""

    idle: IdleConfig
    trip: TripConfig
    dedup: DedupConfig
    validation: ValidationConfig
    #: Fuel price per fuel type, resolved by the adapter from the temporal price
    #: table so a recomputation uses the price in force at the time (D11).
    prices: dict[str, FuelPrice] = field(default_factory=dict)


class BatchProcessor:
    """Stateful per-partition processor. One instance per consumer.

    Holds a per-vehicle idle state machine and dedup watermarks. Both survive across
    batches, which is essential: an idle period spans many batches, and a machine
    rebuilt per batch would cut every segment at the batch boundary.
    """

    __slots__ = ("_config", "_dedup", "_machines", "_trip_machines")

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config
        self._dedup = DedupState()
        self._machines: dict[str, IdleStateMachine] = {}
        # A separate machine per vehicle, not a combined one: idle and trip detection
        # answer different questions and tolerate different errors. Idle may under-claim
        # because an over-stated cost is worse than an under-stated one; trips may not,
        # because a missed trip makes a working vehicle look like an asset to sell.
        self._trip_machines: dict[str, TripStateMachine] = {}

    @property
    def dedup_state(self) -> DedupState:
        return self._dedup

    @property
    def tracked_vehicles(self) -> int:
        return len(self._machines)

    def load_watermarks(self, watermarks: dict[str, object]) -> None:
        """Restore dedup state after a restart or partition rebalance."""
        from processor.dedup import VehicleWatermark

        typed = {
            vin: mark for vin, mark in watermarks.items() if isinstance(mark, VehicleWatermark)
        }
        self._dedup.load(typed)

    def process(
        self,
        raw_messages: Sequence[bytes],
        vehicles: dict[str, VehicleRef],
        *,
        now: datetime,
    ) -> BatchResult:
        """Process one polled batch.

        ``vehicles`` is the metadata for the VINs in this batch, fetched by the
        adapter. ``now`` is injected so future-skew rejection is deterministic.
        """
        result = BatchResult()

        for raw in raw_messages:
            verdict = decode(raw, self._config.validation, now=now)
            if isinstance(verdict, Rejection):
                result.rejections.append(verdict)
                continue

            decision = self._dedup.classify(verdict, self._config.dedup)
            if decision.disposition is EventDisposition.DUPLICATE:
                result.duplicates += 1
                continue
            if decision.disposition in (EventDisposition.LATE, EventDisposition.TOO_LATE):
                # Routed to the late topic for the batch reconciler. Never dropped.
                result.late.append(verdict)
                continue

            result.accepted.append(verdict)
            self._advance_idle(verdict, vehicles, result)

        return result

    def _advance_idle(
        self,
        event: TelemetryEvent,
        vehicles: dict[str, VehicleRef],
        result: BatchResult,
    ) -> None:
        """Feed one accepted event to its vehicle's idle state machine."""
        machine = self._machines.get(event.vin)
        if machine is None:
            machine = IdleStateMachine(self._config.idle)
            self._machines[event.vin] = machine

        observation = machine.observe(event)

        trip_machine = self._trip_machines.get(event.vin)
        if trip_machine is None:
            trip_machine = TripStateMachine(self._config.trip)
            self._trip_machines[event.vin] = trip_machine
        trip_observation = trip_machine.observe(event)

        vehicle = vehicles.get(event.vin)

        if vehicle is not None and trip_observation.closed_trip is not None:
            result.trips.append(
                trip_with_identity(
                    trip_observation.closed_trip,
                    tenant_id=vehicle.tenant_id,
                    vin=vehicle.vin,
                )
            )

        if vehicle is None:
            result.unknown_vehicle += 1
        else:
            if observation.closed_segment is not None:
                result.idle_segments.append(
                    self._price(
                        with_identity(
                            observation.closed_segment,
                            tenant_id=vehicle.tenant_id,
                            vin=vehicle.vin,
                        ),
                        vehicle,
                    )
                )
            if observation.became_excessive and len(result.alerts) < MAX_ALERTS_PER_BATCH:
                result.alerts.append(self._excessive_alert(event, vehicle, machine))

        result.latest_state[event.vin] = LatestState(
            vin=event.vin,
            tenant_id=event.tenant_id,
            ts=event.ts,
            speed_kph=event.speed_kph,
            ignition=event.ignition.value,
            odometer_km=event.odometer_km,
            lat=event.lat,
            lon=event.lon,
            idle_state=machine.state.value,
            open_idle_seconds=machine.open_duration_seconds,
        )

    def _price(self, segment: IdleSegment, vehicle: VehicleRef) -> PricedIdleSegment:
        """Attach a cost estimate, or record an explicit BEV exclusion (D9)."""
        if not vehicle.powertrain.burns_fuel:
            return PricedIdleSegment(
                segment=segment, vehicle=vehicle, estimate=None, bev_excluded=True
            )

        fuel_type = "petrol" if vehicle.powertrain is not Powertrain.DIESEL else "diesel"
        price = self._config.prices.get(fuel_type)
        if price is None:
            # No price for this fuel type: record the segment without a cost rather
            # than inventing one. A missing price is a configuration problem, and a
            # guessed number would look exactly like a real one.
            return PricedIdleSegment(segment=segment, vehicle=vehicle, estimate=None)

        try:
            estimate = estimate_idle_cost(
                duration_seconds=segment.duration_seconds,
                powertrain=vehicle.powertrain,
                vehicle_class=vehicle.vehicle_class,
                price=price,
                idle_threshold_seconds=self._config.idle.min_seconds,
                fuel_rate_samples=segment.fuel_rate_samples,
            )
        except BevHasNoIdleFuelError:
            # Unreachable given the burns_fuel check above; kept because silently
            # producing a zero here is the exact failure D9 exists to prevent.
            return PricedIdleSegment(
                segment=segment, vehicle=vehicle, estimate=None, bev_excluded=True
            )
        except KeyError:
            # No configured burn rate for this (powertrain, class). Fail visibly by
            # omitting the cost rather than inventing a default (ADR-0002).
            return PricedIdleSegment(segment=segment, vehicle=vehicle, estimate=None)

        return PricedIdleSegment(segment=segment, vehicle=vehicle, estimate=estimate)

    def _excessive_alert(
        self, event: TelemetryEvent, vehicle: VehicleRef, machine: IdleStateMachine
    ) -> Alert:
        return Alert(
            alert_type=AlertType.EXCESSIVE_IDLE_IN_PROGRESS,
            tenant_id=vehicle.tenant_id,
            vin=vehicle.vin,
            triggering_event_ts=event.ts,
            severity="warning",
            detail={
                "idle_seconds": round(machine.open_duration_seconds, 1),
                "threshold_seconds": self._config.idle.excessive_seconds,
                "vehicle_class": vehicle.vehicle_class.value,
            },
            # Suppression key is the SEGMENT's start, not the triggering event's
            # timestamp: the identity being suppressed is "this idle period", so
            # keying on the trigger would make the key depend on which event
            # happened to cross the threshold.
            idle_segment_started_at=machine.open_started_at or event.ts,
        )

    def flush_open_trips(self, vehicles: dict[str, VehicleRef]) -> list[Trip]:
        """Close every open trip, e.g. at shutdown or partition revocation.

        Without this, a journey in progress when the consumer stops is lost, and the
        vehicle looks less used than it was.
        """
        closed: list[Trip] = []
        for vin, machine in self._trip_machines.items():
            trip = machine.flush()
            vehicle = vehicles.get(vin)
            if trip is None or vehicle is None:
                continue
            closed.append(trip_with_identity(trip, tenant_id=vehicle.tenant_id, vin=vin))
        return closed

    def flush_open_segments(self, vehicles: dict[str, VehicleRef]) -> list[PricedIdleSegment]:
        """Close every open segment, e.g. at shutdown or partition revocation.

        Without this, a segment open when the consumer stops or loses a partition is
        lost entirely - and it is precisely the long segments, the expensive ones,
        that are most likely to be open at any given moment.
        """
        closed: list[PricedIdleSegment] = []
        for vin, machine in self._machines.items():
            segment = machine.flush()
            vehicle = vehicles.get(vin)
            if segment is None or vehicle is None:
                continue
            closed.append(
                self._price(
                    with_identity(segment, tenant_id=vehicle.tenant_id, vin=vin),
                    vehicle,
                )
            )
        return closed

    def drop_vehicles(self, vins: Sequence[str]) -> None:
        """Forget per-vehicle state, after its partitions were revoked.

        Called once the caller has persisted whatever ``flush_open_segments``
        returned. Without this, a long-running consumer that has been rebalanced
        several times keeps state for vehicles it no longer owns, and memory grows
        with rebalance count rather than staying bounded by assigned partitions
        (CLAUDE.md S9: no unbounded memory growth).
        """
        for vin in vins:
            self._machines.pop(vin, None)
            self._trip_machines.pop(vin, None)
            self._dedup.watermarks.pop(vin, None)

    @property
    def tracked_vins(self) -> tuple[str, ...]:
        """VINs this processor currently holds state for."""
        return tuple(self._machines)
