"""Trip segmentation over noisy telemetry.

The complement of idle detection: idle segmentation finds the periods a vehicle was
stationary with the engine running, this finds the periods it was actually moving. The
hackathon PDF S9 names "segmenting noisy GPS into stops and trips" explicitly, and the
3NF core in S8 lists Trip as a first-class entity.

It is not merely symmetric with idle detection, because the two answer different
questions and therefore tolerate different errors:

*Idle detection under-claims on purpose* - a cost figure that over-states is worse than
one that under-states, so a segment counts only observed time.

*Trip detection must not under-claim*, because its output is the denominator of the
utilisation score (D3). Miss a trip and a working vehicle looks under-used, which would
send a fleet manager to sell an asset they need.

So a trip survives brief stops. A vehicle waiting at a traffic light has not finished
its trip, and splitting there would turn one delivery run into fifteen, inflating the
trip count and destroying the active-day signal utilisation depends on.

Pure domain code: no I/O, no clock, no randomness (CLAUDE.md S7).
Complexity: O(1) state and O(1) work per event; O(n) over a stream.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from fleet_common.events import TelemetryEvent

# Values are PROPOSED defaults (D3). Config-driven in the services.
DEFAULT_MOVE_SPEED_KPH: Final[float] = 5.0
DEFAULT_STOP_SPEED_KPH: Final[float] = 2.0
#: A stop shorter than this does not end a trip. Traffic lights, junctions and brief
#: obstructions are part of a journey, not the end of one.
DEFAULT_MIN_STOP_SECONDS: Final[int] = 300
#: A trip shorter than this is manoeuvring - repositioning in a yard, not a journey.
DEFAULT_MIN_TRIP_SECONDS: Final[int] = 120
DEFAULT_MIN_TRIP_DISTANCE_KM: Final[float] = 0.5
#: Beyond this, unobserved time cannot be attributed to the trip.
DEFAULT_GAP_TOLERANCE_SECONDS: Final[int] = 300


class TripState(StrEnum):
    STOPPED = "stopped"
    MOVING = "moving"
    #: Moving, but currently stationary within the tolerated stop window. The trip is
    #: still open; only a long enough stop closes it.
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class TripConfig:
    """Tunable trip-detection parameters (D3)."""

    move_speed_kph: float = DEFAULT_MOVE_SPEED_KPH
    stop_speed_kph: float = DEFAULT_STOP_SPEED_KPH
    min_stop_seconds: int = DEFAULT_MIN_STOP_SECONDS
    min_trip_seconds: int = DEFAULT_MIN_TRIP_SECONDS
    min_trip_distance_km: float = DEFAULT_MIN_TRIP_DISTANCE_KM
    gap_tolerance_seconds: int = DEFAULT_GAP_TOLERANCE_SECONDS

    def __post_init__(self) -> None:
        if self.move_speed_kph < self.stop_speed_kph:
            raise ValueError(
                "move_speed_kph must be >= stop_speed_kph, otherwise the state machine "
                "flaps between moving and stopped on sensor noise"
            )
        if self.min_stop_seconds <= 0:
            raise ValueError("min_stop_seconds must be positive")
        if self.min_trip_seconds <= 0:
            raise ValueError("min_trip_seconds must be positive")
        if self.min_trip_distance_km < 0:
            raise ValueError("min_trip_distance_km must be non-negative")
        if self.gap_tolerance_seconds < 0:
            raise ValueError("gap_tolerance_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class Trip:
    """One completed journey.

    ``moving_seconds`` excludes the paused time inside the trip, while
    ``duration_seconds`` includes it. Utilisation needs the first (how long the vehicle
    actually worked); a driver's shift report would want the second.
    """

    tenant_id: str
    vin: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    moving_seconds: float
    idle_seconds: float
    distance_km: float
    sample_count: int
    max_gap_seconds: float
    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None

    @property
    def average_speed_kph(self) -> float:
        """Average over moving time, not elapsed time.

        Elapsed time would include the pauses, making every urban trip look slow and
        hiding a genuinely slow one.
        """
        if self.moving_seconds <= 0:
            return 0.0
        return self.distance_km / (self.moving_seconds / 3600.0)


@dataclass(slots=True)
class _OpenTrip:
    """Mutable accumulator for the trip being built."""

    started_at: datetime
    last_ts: datetime
    start_odometer_km: float
    last_odometer_km: float
    sample_count: int
    moving_seconds: float = 0.0
    idle_seconds: float = 0.0
    max_gap_seconds: float = 0.0
    start_lat: float | None = None
    start_lon: float | None = None
    last_lat: float | None = None
    last_lon: float | None = None
    #: When the current pause began, or None while moving. A pause only ends the trip
    #: once it exceeds ``min_stop_seconds``.
    paused_since: datetime | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.last_ts - self.started_at).total_seconds()

    @property
    def distance_km(self) -> float:
        # Odometer delta rather than summed GPS legs: an odometer is monotonic and does
        # not accumulate the error that integrating noisy fixes would.
        return max(0.0, self.last_odometer_km - self.start_odometer_km)


@dataclass(frozen=True, slots=True)
class TripObservation:
    """Result of feeding one event to the trip state machine."""

    closed_trip: Trip | None = None
    out_of_order: bool = False


class TripStateMachine:
    """Per-vehicle incremental trip detector.

    One instance per VIN, mirroring ``IdleStateMachine``. Events must arrive in
    event-time order; anything older is reported rather than applied.
    """

    __slots__ = ("_config", "_last_ts", "_open", "_state")

    def __init__(self, config: TripConfig | None = None) -> None:
        self._config = config or TripConfig()
        self._open: _OpenTrip | None = None
        self._state = TripState.STOPPED
        self._last_ts: datetime | None = None

    @property
    def state(self) -> TripState:
        return self._state

    @property
    def open_distance_km(self) -> float:
        return self._open.distance_km if self._open is not None else 0.0

    def _is_moving(self, event: TelemetryEvent) -> bool:
        """Hysteresis: starting a trip needs more speed than continuing one."""
        bound = (
            self._config.stop_speed_kph
            if self._state in (TripState.MOVING, TripState.PAUSED)
            else self._config.move_speed_kph
        )
        return event.speed_kph > bound

    def observe(self, event: TelemetryEvent) -> TripObservation:
        """Feed one sample. Returns what that sample produced."""
        if self._last_ts is not None and event.ts <= self._last_ts:
            return TripObservation(out_of_order=True)

        gap = (event.ts - self._last_ts).total_seconds() if self._last_ts is not None else 0.0
        self._last_ts = event.ts
        moving = self._is_moving(event)

        # Beyond tolerance the unobserved time cannot be attributed to the trip, and the
        # odometer may have advanced a long way unseen.
        if self._open is not None and gap > self._config.gap_tolerance_seconds:
            closed = self._close()
            if moving:
                self._start(event)
            return TripObservation(closed_trip=closed)

        if moving:
            if self._open is None:
                self._start(event)
            else:
                self._accumulate(event, gap, moving=True)
            return TripObservation()

        # Stationary. An open trip pauses rather than ending, until the pause is long
        # enough to count as a real stop.
        if self._open is not None:
            return self._handle_pause(event, gap)

        self._state = TripState.STOPPED
        return TripObservation()

    def _handle_pause(self, event: TelemetryEvent, gap: float) -> TripObservation:
        open_trip = self._open
        assert open_trip is not None

        if open_trip.paused_since is None:
            open_trip.paused_since = event.ts
            self._state = TripState.PAUSED

        paused_for = (event.ts - open_trip.paused_since).total_seconds()
        if paused_for >= self._config.min_stop_seconds:
            # A real stop. The trip ends where the vehicle actually stopped, not where
            # we noticed - counting the whole pause as trip time would inflate duration
            # and drag the average speed down.
            open_trip.last_ts = open_trip.paused_since
            return TripObservation(closed_trip=self._close())

        self._accumulate(event, gap, moving=False)
        return TripObservation()

    def _start(self, event: TelemetryEvent) -> None:
        self._open = _OpenTrip(
            started_at=event.ts,
            last_ts=event.ts,
            start_odometer_km=event.odometer_km,
            last_odometer_km=event.odometer_km,
            sample_count=1,
            start_lat=event.lat,
            start_lon=event.lon,
            last_lat=event.lat,
            last_lon=event.lon,
        )
        self._state = TripState.MOVING

    def _accumulate(self, event: TelemetryEvent, gap: float, *, moving: bool) -> None:
        open_trip = self._open
        assert open_trip is not None
        open_trip.last_ts = event.ts
        open_trip.last_odometer_km = max(open_trip.last_odometer_km, event.odometer_km)
        open_trip.sample_count += 1
        open_trip.max_gap_seconds = max(open_trip.max_gap_seconds, gap)
        if event.lat is not None and event.lon is not None:
            open_trip.last_lat = event.lat
            open_trip.last_lon = event.lon

        if moving:
            open_trip.moving_seconds += gap
            open_trip.paused_since = None
            self._state = TripState.MOVING
        else:
            open_trip.idle_seconds += gap

    def flush(self) -> Trip | None:
        """Close any open trip, e.g. at shutdown or end of a backfill batch."""
        return self._close() if self._open is not None else None

    def _close(self) -> Trip | None:
        """Finalise the open trip, or discard it if it was only manoeuvring."""
        open_trip = self._open
        self._open = None
        self._state = TripState.STOPPED
        if open_trip is None:
            return None

        duration = open_trip.duration_seconds
        distance = open_trip.distance_km

        # Both bounds must be met. Time alone would count a vehicle shunting around a
        # yard for ten minutes as a journey; distance alone would count a tow.
        if duration < self._config.min_trip_seconds:
            return None
        if distance < self._config.min_trip_distance_km:
            return None

        return Trip(
            # Identity is stamped by the caller, which owns tenant context.
            tenant_id="",
            vin="",
            started_at=open_trip.started_at,
            ended_at=open_trip.last_ts,
            duration_seconds=duration,
            moving_seconds=open_trip.moving_seconds,
            idle_seconds=open_trip.idle_seconds,
            distance_km=distance,
            sample_count=open_trip.sample_count,
            max_gap_seconds=open_trip.max_gap_seconds,
            start_lat=open_trip.start_lat,
            start_lon=open_trip.start_lon,
            end_lat=open_trip.last_lat,
            end_lon=open_trip.last_lon,
        )


def with_identity(trip: Trip, *, tenant_id: str, vin: str) -> Trip:
    """Stamp tenant and VIN onto a trip produced by the state machine."""
    return Trip(
        tenant_id=tenant_id,
        vin=vin,
        started_at=trip.started_at,
        ended_at=trip.ended_at,
        duration_seconds=trip.duration_seconds,
        moving_seconds=trip.moving_seconds,
        idle_seconds=trip.idle_seconds,
        distance_km=trip.distance_km,
        sample_count=trip.sample_count,
        max_gap_seconds=trip.max_gap_seconds,
        start_lat=trip.start_lat,
        start_lon=trip.start_lon,
        end_lat=trip.end_lat,
        end_lon=trip.end_lon,
    )


def segment_trips(events: Iterable[TelemetryEvent], config: TripConfig | None = None) -> list[Trip]:
    """Batch helper: fold a per-vehicle stream into completed trips.

    Delegates to ``TripStateMachine`` so batch and streaming cannot drift apart.
    """
    machine = TripStateMachine(config)
    trips: list[Trip] = []
    identity: tuple[str, str] | None = None

    for event in events:
        if identity is None:
            identity = (event.tenant_id, event.vin)
        observation = machine.observe(event)
        if observation.closed_trip is not None:
            trips.append(observation.closed_trip)

    final = machine.flush()
    if final is not None:
        trips.append(final)

    if identity is None:
        return []
    tenant_id, vin = identity
    return [with_identity(t, tenant_id=tenant_id, vin=vin) for t in trips]


@dataclass(frozen=True, slots=True)
class DailyActivity:
    """One vehicle's activity on one local calendar day.

    The shape ``VehicleActivity`` in the utilisation domain needs, aggregated from
    trips and idle events by the batch job.
    """

    vin: str
    day: str
    trip_count: int
    moving_seconds: float
    distance_km: float
    idle_seconds: float

    @property
    def was_active(self) -> bool:
        """Whether this counts as an active day for the utilisation score (D3)."""
        return self.trip_count > 0


def summarise_day(vin: str, day: str, trips: list[Trip], idle_seconds: float) -> DailyActivity:
    """Aggregate one vehicle-day. Pure, so the rollup logic is testable without a database."""
    return DailyActivity(
        vin=vin,
        day=day,
        trip_count=len(trips),
        moving_seconds=sum(t.moving_seconds for t in trips),
        distance_km=sum(t.distance_km for t in trips),
        idle_seconds=idle_seconds,
    )
