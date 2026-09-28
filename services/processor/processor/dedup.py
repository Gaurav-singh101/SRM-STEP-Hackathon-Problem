"""Streaming de-duplication and lateness classification.

Delivery is at-least-once, so duplicates are normal rather than exceptional
(CLAUDE.md S8). The requirement is that they cost nothing: a replayed batch must not
double-count fuel.

**Why a per-vehicle high-water mark rather than a key per event.** CLAUDE.md S8
forbids storing one Redis key per event, and at 100K events/s the reason is
arithmetic: one key per event with a 10-minute window is 60 million keys, tens of
gigabytes of Redis, for a question that only ever concerns one vehicle at a time.
A high-water mark is one small record per *vehicle* - 100,000 records, bounded and
constant regardless of event rate.

The mark is not sufficient on its own, and is not claimed to be: it is the fast
path. A unique index on ``(vin, started_at)`` in Postgres is the guarantee. Together
they give an effectively-once outcome from at-least-once delivery.

**The mark alone is not enough, and a test proved it.** With only a high-water mark,
a redelivered batch - the ordinary consequence of an offset commit failing - produces
one duplicate and then N-1 events that look *older than the mark*, which is
indistinguishable from genuine out-of-order data. Those were routed to the late topic,
so pure duplicates reached the batch reconciler and risked being counted again.

So each vehicle also carries a **sliding replay window**: a 64-bit mask of which
recent sequence numbers below the mark have already been seen. This is the
anti-replay window used by IPsec, and it makes dedup *exact* for anything inside the
window while costing one extra integer per vehicle. CLAUDE.md S8 names exactly this
shape - "per-vehicle high-water mark / small recent-window per vehicle plus a unique
key in the sink".

Outside the window the mark cannot distinguish the two cases, so the event goes to
the late topic and the unique index on ``(vin, started_at)`` in Postgres is what
prevents double-counting. That index is the guarantee; all of this is the fast path.

**Why not a Bloom filter.** PDF S9 offers Bloom filters for streaming dedup, and for
an unkeyed stream that would be the right structure. This stream is keyed by VIN with
a monotonic per-vehicle ``seq``, which makes exact O(1) dedup possible - and exact
beats probabilistic at the same cost. A Bloom filter's false positives would silently
discard real events, which for a billing figure is the one error we cannot accept.

Pure: all state is passed in and returned. The Redis adapter persists it.
Complexity: O(1) per event, O(1) state per vehicle.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from fleet_common.events import TelemetryEvent

#: Width of the per-vehicle replay window, in sequence numbers. 64 fits in one
#: integer and covers far more than any realistic reordering or redelivery burst:
#: at a 10 s sample interval it is over ten minutes of one vehicle's history.
REPLAY_WINDOW: Final[int] = 64
_WINDOW_MASK: Final[int] = (1 << REPLAY_WINDOW) - 1


class EventDisposition(StrEnum):
    """What should happen to an event after dedup and lateness checks."""

    #: Normal: in order and not seen before.
    ACCEPT = "accept"
    #: Already seen. Counted, then discarded.
    DUPLICATE = "duplicate"
    #: Older than the high-water mark but inside the lateness window. Still usable
    #: by the batch reconciler, so it goes to the late topic rather than the bin.
    LATE = "late"
    #: Older than the lateness window. Also goes to the late topic - "too late to
    #: process in the stream" is not the same as "may be thrown away"
    #: (CLAUDE.md S8: never silently drop).
    TOO_LATE = "too_late"


@dataclass(frozen=True, slots=True)
class VehicleWatermark:
    """Per-vehicle dedup and ordering state.

    Small and fixed-size on purpose: this is what makes the approach scale. Four
    scalars per vehicle, not a set of seen ids.
    """

    vin: str
    #: Highest ``seq`` accepted for this vehicle.
    highest_seq: int
    #: Event time of the highest accepted event, used for lateness decisions.
    highest_ts: datetime
    #: Sliding replay window. Bit *i* set means ``highest_seq - i`` has been seen,
    #: so bit 0 is always set. Makes dedup exact within ``REPLAY_WINDOW``.
    seen_mask: int = 1
    #: Running counters, useful as metrics and for the dedup-rate figure the load
    #: test reports.
    accepted: int = 0
    duplicates: int = 0
    late: int = 0
    #: Events too old for the window to decide on. Worth its own counter: a rising
    #: value means reordering exceeds the window and it should be widened.
    beyond_window: int = 0

    @classmethod
    def initial(cls, event: TelemetryEvent) -> VehicleWatermark:
        return cls(
            vin=event.vin,
            highest_seq=event.seq,
            highest_ts=event.ts,
            seen_mask=1,
            accepted=1,
        )

    def has_seen(self, seq: int) -> bool:
        """Whether ``seq`` is inside the window and already marked seen."""
        offset = self.highest_seq - seq
        if offset < 0 or offset >= REPLAY_WINDOW:
            return False
        return bool(self.seen_mask >> offset & 1)

    def advanced_to(self, seq: int) -> int:
        """Window mask after accepting ``seq`` as the new highest."""
        shift = seq - self.highest_seq
        if shift >= REPLAY_WINDOW:
            # The jump is wider than the window, so nothing older remains in range.
            return 1
        return ((self.seen_mask << shift) | 1) & _WINDOW_MASK

    def marked(self, seq: int) -> int:
        """Window mask with ``seq`` recorded as seen, for an in-window backfill."""
        offset = self.highest_seq - seq
        if offset < 0 or offset >= REPLAY_WINDOW:
            return self.seen_mask
        return self.seen_mask | (1 << offset)


@dataclass(frozen=True, slots=True)
class DedupConfig:
    """Lateness bounds (CLAUDE.md S8)."""

    #: Events older than the watermark by up to this much are late but reconcilable.
    allowed_lateness_seconds: int = 300

    def __post_init__(self) -> None:
        if self.allowed_lateness_seconds < 0:
            raise ValueError("allowed_lateness_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class DedupDecision:
    """Outcome for one event, plus the state to carry forward."""

    disposition: EventDisposition
    watermark: VehicleWatermark

    @property
    def accepted(self) -> bool:
        return self.disposition is EventDisposition.ACCEPT


def classify(
    event: TelemetryEvent,
    watermark: VehicleWatermark | None,
    config: DedupConfig,
) -> DedupDecision:
    """Decide what to do with one event given this vehicle's watermark.

    First event for a vehicle is always accepted: there is nothing to compare to.
    """
    if watermark is None:
        return DedupDecision(EventDisposition.ACCEPT, VehicleWatermark.initial(event))

    if event.seq > watermark.highest_seq:
        return DedupDecision(
            EventDisposition.ACCEPT,
            replace(
                watermark,
                highest_seq=event.seq,
                # max() rather than assignment: seq is authoritative for ordering,
                # but a device with a briefly wrong clock must not be able to drag
                # the timestamp watermark backwards and make later events look late.
                highest_ts=max(watermark.highest_ts, event.ts),
                seen_mask=watermark.advanced_to(event.seq),
                accepted=watermark.accepted + 1,
            ),
        )

    # At or below the mark. The window decides whether this is a redelivery of
    # something already handled or genuinely new data arriving out of order.
    if watermark.has_seen(event.seq):
        return DedupDecision(
            EventDisposition.DUPLICATE,
            replace(watermark, duplicates=watermark.duplicates + 1),
        )

    offset = watermark.highest_seq - event.seq
    if offset >= REPLAY_WINDOW:
        # Too old for the window to judge. Send it to the late topic and let the
        # unique index in the sink be the guarantee - the honest option, since
        # guessing either way risks losing data or counting it twice.
        return DedupDecision(
            EventDisposition.TOO_LATE,
            replace(
                watermark,
                late=watermark.late + 1,
                beyond_window=watermark.beyond_window + 1,
            ),
        )

    # Genuinely new, but out of order: the idle state machine is a sequential fold
    # and cannot take it, so the batch reconciler does. Marked seen so a later
    # redelivery of this same event is recognised as a duplicate.
    behind = watermark.highest_ts - event.ts
    disposition = (
        EventDisposition.LATE
        if behind <= timedelta(seconds=config.allowed_lateness_seconds)
        else EventDisposition.TOO_LATE
    )
    return DedupDecision(
        disposition,
        replace(
            watermark,
            seen_mask=watermark.marked(event.seq),
            late=watermark.late + 1,
        ),
    )


@dataclass(slots=True)
class DedupState:
    """Watermarks for the vehicles this consumer instance has seen.

    Bounded by the number of *vehicles on assigned partitions*, not by event rate,
    which is what keeps memory flat under load. On rebalance the adapter reloads the
    marks for its new partitions from Redis.
    """

    watermarks: dict[str, VehicleWatermark] = field(default_factory=dict)

    def classify(self, event: TelemetryEvent, config: DedupConfig) -> DedupDecision:
        decision = classify(event, self.watermarks.get(event.vin), config)
        self.watermarks[event.vin] = decision.watermark
        return decision

    def load(self, watermarks: dict[str, VehicleWatermark]) -> None:
        """Seed from persisted state, e.g. after a restart or partition rebalance.

        Without this, a restart would re-accept everything inside the retention
        window as new and double-count it.
        """
        self.watermarks.update(watermarks)

    @property
    def vehicle_count(self) -> int:
        return len(self.watermarks)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "accepted": sum(w.accepted for w in self.watermarks.values()),
            "duplicates": sum(w.duplicates for w in self.watermarks.values()),
            "late": sum(w.late for w in self.watermarks.values()),
            "beyond_window": sum(w.beyond_window for w in self.watermarks.values()),
        }
