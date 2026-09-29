"""Idle segmentation over noisy telemetry (D1, ADR-0001).

Turns a per-vehicle stream of samples into closed idle segments. This is the
algorithm the whole product rests on: every fuel and cost figure is derived from
the segments produced here.

Pure domain code - no I/O, no clock, no randomness (CLAUDE.md S7). The caller
supplies events in event-time order; time comes only from ``event.ts``.

Two behaviours matter more than the thresholds themselves:

*Hysteresis.* Entering idle uses a lower speed bound than leaving it. With a
single bound, GPS jitter around the threshold flaps the state machine and
shatters one real idle period into many short ones.

*Gap tolerance.* Samples go missing (tunnels, dropped uplinks). Bridging gaps up
to a bound keeps one real idle period intact; a gap beyond the bound closes the
segment, because we cannot honestly claim the engine ran through unobserved
time.

Complexity: O(1) state and O(1) work per event, O(n) over a stream of n events.
Memory is bounded per vehicle regardless of segment length, which is what lets
it run at 100K events/s (CLAUDE.md S9).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from fleet_common.events import TelemetryEvent

# Values are PROPOSED defaults (D1). Real values come from config; no magic
# numbers are permitted in business logic (CLAUDE.md S5).
DEFAULT_MIN_SECONDS: Final[int] = 180
DEFAULT_ENTRY_SPEED_KPH: Final[float] = 2.0
DEFAULT_EXIT_SPEED_KPH: Final[float] = 5.0
DEFAULT_EXCESSIVE_SECONDS: Final[int] = 600
DEFAULT_GAP_TOLERANCE_SECONDS: Final[int] = 30


class IdleState(StrEnum):
    MOVING = "moving"
    # Stationary with the engine powered, but not yet past ``min_seconds``.
    CANDIDATE = "candidate"
    # Past ``min_seconds``: this is billable idle.
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class IdleConfig:
    """Tunable idle-detection parameters (D1). All injected, never hard-coded."""

    min_seconds: int = DEFAULT_MIN_SECONDS
    entry_speed_kph: float = DEFAULT_ENTRY_SPEED_KPH
    exit_speed_kph: float = DEFAULT_EXIT_SPEED_KPH
    excessive_seconds: int = DEFAULT_EXCESSIVE_SECONDS
    gap_tolerance_seconds: int = DEFAULT_GAP_TOLERANCE_SECONDS
    # Optional exclusion, default off (D1). A reefer or crane running on PTO is
    # doing productive work, so a finance user may not want it billed as waste.
    exclude_pto_active: bool = False

    def __post_init__(self) -> None:
        if self.exit_speed_kph < self.entry_speed_kph:
            raise ValueError(
                "exit_speed_kph must be >= entry_speed_kph, otherwise the state "
                "machine flaps on sensor noise"
            )
        if self.min_seconds <= 0:
            raise ValueError("min_seconds must be positive")
        if self.excessive_seconds < self.min_seconds:
            raise ValueError("excessive_seconds must be >= min_seconds")
        if self.gap_tolerance_seconds < 0:
            raise ValueError("gap_tolerance_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class IdleSegment:
    """One closed period of engine-on, stationary time for one vehicle.

    ``duration_seconds`` spans first to last observed sample, so it never counts
    unobserved time. That makes the figure conservative by construction: a cost
    estimate derived from it under-claims rather than over-claims.
    """

    tenant_id: str
    vin: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    sample_count: int
    start_odometer_km: float
    # Largest bridged gap. Surfaced as a data-quality signal so a user can see
    # when a segment rests on sparse samples.
    max_gap_seconds: float
    excessive: bool
    # Observed instantaneous consumption, for the D2 measured cross-check.
    fuel_rate_samples: tuple[float, ...]
    pto_active_any: bool
    start_lat: float | None
    start_lon: float | None

    @property
    def duration_hours(self) -> float:
        return self.duration_seconds / 3600.0


@dataclass(frozen=True, slots=True)
class IdleObservation:
    """Result of feeding one event to the state machine."""

    # Set when this event ended a segment that met ``min_seconds``.
    closed_segment: IdleSegment | None = None
    # True only on the single event that crosses ``excessive_seconds`` while the
    # vehicle is still idling. Drives the EXCESSIVE_IDLE_IN_PROGRESS alert (D10)
    # and fires once per segment, so a long idle cannot storm alerts.
    became_excessive: bool = False
    # True when the event was ignored as out of order (ts <= last seen ts). The
    # caller routes these to the late topic; they are never silently dropped.
    out_of_order: bool = False


@dataclass(slots=True)
class _OpenSegment:
    """Mutable accumulator for the segment currently being built."""

    started_at: datetime
    last_ts: datetime
    sample_count: int
    start_odometer_km: float
    max_gap_seconds: float
    fuel_rate_samples: list[float] = field(default_factory=list)
    pto_active_any: bool = False
    start_lat: float | None = None
    start_lon: float | None = None
    excessive_emitted: bool = False

    @property
    def duration_seconds(self) -> float:
        return (self.last_ts - self.started_at).total_seconds()


class IdleStateMachine:
    """Per-vehicle incremental idle detector.

    One instance per VIN. The processor keeps these keyed by VIN and checkpoints
    them to Redis, so a restart resumes mid-segment instead of losing it.

    Events must arrive in event-time order. Anything older than the last seen
    sample is reported via ``IdleObservation.out_of_order`` for the caller to
    route to the late topic (CLAUDE.md S8) - the state machine does not rewrite
    history it has already emitted.
    """

    __slots__ = ("_config", "_last_ts", "_open", "_state")

    def __init__(self, config: IdleConfig | None = None) -> None:
        self._config = config or IdleConfig()
        self._open: _OpenSegment | None = None
        self._state = IdleState.MOVING
        self._last_ts: datetime | None = None

    @property
    def state(self) -> IdleState:
        return self._state

    @property
    def open_duration_seconds(self) -> float:
        """Duration of the in-progress segment, 0.0 when none is open."""
        return self._open.duration_seconds if self._open is not None else 0.0

    @property
    def open_started_at(self) -> datetime | None:
        """Start of the in-progress segment, or ``None`` when none is open.

        Exposed so an alert can be keyed on segment identity rather than on the
        timestamp of whichever event happened to trigger it (D10 suppression).
        """
        return self._open.started_at if self._open is not None else None

    def _is_idling(self, event: TelemetryEvent) -> bool:
        """Apply hysteresis: the bound depends on whether we are already idle."""
        if not event.engine_powered:
            return False
        if self._config.exclude_pto_active and event.pto_active:
            return False
        bound = (
            self._config.exit_speed_kph
            if self._state in (IdleState.CANDIDATE, IdleState.CONFIRMED)
            else self._config.entry_speed_kph
        )
        return event.speed_kph <= bound

    def observe(self, event: TelemetryEvent) -> IdleObservation:
        """Feed one sample. Returns what that sample produced."""
        if self._last_ts is not None and event.ts <= self._last_ts:
            return IdleObservation(out_of_order=True)

        gap = (event.ts - self._last_ts).total_seconds() if self._last_ts is not None else 0.0
        self._last_ts = event.ts
        idling = self._is_idling(event)

        # A gap beyond tolerance means we cannot vouch for the unobserved time.
        # Close what we have at the last confirmed sample, then start fresh.
        if self._open is not None and gap > self._config.gap_tolerance_seconds:
            closed = self._close()
            if idling:
                self._start(event)
            return IdleObservation(closed_segment=closed)

        if idling:
            if self._open is None:
                self._start(event)
                return IdleObservation()
            self._accumulate(event, gap)
            return IdleObservation(became_excessive=self._check_excessive())

        # Moving again: close any open segment at this sample.
        if self._open is not None:
            self._open.last_ts = event.ts
            closed = self._close()
            return IdleObservation(closed_segment=closed)

        self._state = IdleState.MOVING
        return IdleObservation()

    def flush(self) -> IdleSegment | None:
        """Close any open segment, e.g. at shutdown or end of a backfill batch.

        Without this, a segment still open when the consumer stops is lost.
        """
        return self._close() if self._open is not None else None

    def _start(self, event: TelemetryEvent) -> None:
        self._open = _OpenSegment(
            started_at=event.ts,
            last_ts=event.ts,
            sample_count=1,
            start_odometer_km=event.odometer_km,
            max_gap_seconds=0.0,
            fuel_rate_samples=[event.fuel_rate_lph] if event.fuel_rate_lph is not None else [],
            pto_active_any=bool(event.pto_active),
            start_lat=event.lat,
            start_lon=event.lon,
        )
        self._state = IdleState.CANDIDATE

    def _accumulate(self, event: TelemetryEvent, gap: float) -> None:
        open_seg = self._open
        assert open_seg is not None
        open_seg.last_ts = event.ts
        open_seg.sample_count += 1
        open_seg.max_gap_seconds = max(open_seg.max_gap_seconds, gap)
        if event.fuel_rate_lph is not None:
            open_seg.fuel_rate_samples.append(event.fuel_rate_lph)
        if event.pto_active:
            open_seg.pto_active_any = True
        if open_seg.duration_seconds >= self._config.min_seconds:
            self._state = IdleState.CONFIRMED

    def _check_excessive(self) -> bool:
        """Emit the excessive-idle transition exactly once per segment."""
        open_seg = self._open
        assert open_seg is not None
        if open_seg.excessive_emitted:
            return False
        if open_seg.duration_seconds >= self._config.excessive_seconds:
            open_seg.excessive_emitted = True
            return True
        return False

    def _close(self) -> IdleSegment | None:
        """Finalise the open segment, or discard it if it never met min_seconds."""
        open_seg = self._open
        self._open = None
        self._state = IdleState.MOVING
        if open_seg is None:
            return None
        duration = open_seg.duration_seconds
        if duration < self._config.min_seconds:
            # Below the threshold this is unavoidable idling (traffic lights,
            # loading bays) and is deliberately not billed (D1).
            return None
        return IdleSegment(
            # Identity is stamped by the caller, which owns tenant context.
            tenant_id="",
            vin="",
            started_at=open_seg.started_at,
            ended_at=open_seg.last_ts,
            duration_seconds=duration,
            sample_count=open_seg.sample_count,
            start_odometer_km=open_seg.start_odometer_km,
            max_gap_seconds=open_seg.max_gap_seconds,
            excessive=duration >= self._config.excessive_seconds,
            fuel_rate_samples=tuple(open_seg.fuel_rate_samples),
            pto_active_any=open_seg.pto_active_any,
            start_lat=open_seg.start_lat,
            start_lon=open_seg.start_lon,
        )


def with_identity(segment: IdleSegment, *, tenant_id: str, vin: str) -> IdleSegment:
    """Stamp tenant and VIN onto a segment produced by the state machine."""
    return IdleSegment(
        tenant_id=tenant_id,
        vin=vin,
        started_at=segment.started_at,
        ended_at=segment.ended_at,
        duration_seconds=segment.duration_seconds,
        sample_count=segment.sample_count,
        start_odometer_km=segment.start_odometer_km,
        max_gap_seconds=segment.max_gap_seconds,
        excessive=segment.excessive,
        fuel_rate_samples=segment.fuel_rate_samples,
        pto_active_any=segment.pto_active_any,
        start_lat=segment.start_lat,
        start_lon=segment.start_lon,
    )


def segment_idle(
    events: Iterable[TelemetryEvent], config: IdleConfig | None = None
) -> list[IdleSegment]:
    """Batch helper: fold a whole per-vehicle stream into closed segments.

    Used by tests and the backfill job. It delegates to ``IdleStateMachine`` so
    batch and streaming results cannot drift apart - there is only one
    implementation of the rules.
    """
    machine = IdleStateMachine(config)
    segments: list[IdleSegment] = []
    identity: tuple[str, str] | None = None

    for event in events:
        if identity is None:
            identity = (event.tenant_id, event.vin)
        observation = machine.observe(event)
        if observation.closed_segment is not None:
            segments.append(observation.closed_segment)

    final = machine.flush()
    if final is not None:
        segments.append(final)

    if identity is None:
        return []
    tenant_id, vin = identity
    return [with_identity(s, tenant_id=tenant_id, vin=vin) for s in segments]
