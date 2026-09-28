"""Tests for de-duplication and lateness classification.

The invariant that matters: **an event already handled must never be handled again,
and an event never handled must never be discarded.** Everything here exists to hold
one or the other side of that line.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.testing import make_event
from processor.dedup import (
    REPLAY_WINDOW,
    DedupConfig,
    DedupState,
    EventDisposition,
    VehicleWatermark,
    classify,
)

CONFIG = DedupConfig(allowed_lateness_seconds=300)


def at(seq: int, offset: float) -> object:
    return make_event(seq=seq, offset_seconds=offset, speed_kph=0.0)


# --------------------------------------------------------------------------
# First sight and forward progress
# --------------------------------------------------------------------------


def test_first_event_for_a_vehicle_is_accepted() -> None:
    decision = classify(at(5, 0), None, CONFIG)  # type: ignore[arg-type]
    assert decision.disposition is EventDisposition.ACCEPT
    assert decision.watermark.highest_seq == 5


def test_increasing_sequence_is_accepted() -> None:
    watermark = VehicleWatermark.initial(at(1, 0))  # type: ignore[arg-type]
    decision = classify(at(2, 10), watermark, CONFIG)  # type: ignore[arg-type]
    assert decision.accepted
    assert decision.watermark.highest_seq == 2


def test_a_gap_in_sequence_numbers_is_accepted() -> None:
    """Dropped uplinks leave gaps; a gap is missing data, not bad data."""
    watermark = VehicleWatermark.initial(at(1, 0))  # type: ignore[arg-type]
    decision = classify(at(50, 500), watermark, CONFIG)  # type: ignore[arg-type]
    assert decision.accepted
    assert decision.watermark.highest_seq == 50


# --------------------------------------------------------------------------
# Redelivery: the case a bare high-water mark got wrong
# --------------------------------------------------------------------------


def test_replaying_the_same_event_is_a_duplicate() -> None:
    watermark = VehicleWatermark.initial(at(1, 0))  # type: ignore[arg-type]
    assert classify(at(1, 0), watermark, CONFIG).disposition is EventDisposition.DUPLICATE  # type: ignore[arg-type]


def test_replaying_a_whole_batch_yields_only_duplicates() -> None:
    """The case that exposed the need for a window.

    A failed offset commit redelivers the entire batch. With only a high-water mark,
    every event except the last looked older than the mark and was routed to the late
    topic - sending pure duplicates to the batch reconciler.
    """
    state = DedupState()
    batch = [at(seq, seq * 10) for seq in range(1, 21)]

    first = [state.classify(e, CONFIG).disposition for e in batch]  # type: ignore[arg-type]
    replay = [state.classify(e, CONFIG).disposition for e in batch]  # type: ignore[arg-type]

    assert all(d is EventDisposition.ACCEPT for d in first)
    assert all(d is EventDisposition.DUPLICATE for d in replay), (
        f"replay produced {set(replay)}, expected only duplicates"
    )


def test_replay_is_exact_across_the_whole_window() -> None:
    state = DedupState()
    batch = [at(seq, seq * 10) for seq in range(1, REPLAY_WINDOW + 1)]
    for event in batch:
        state.classify(event, CONFIG)  # type: ignore[arg-type]

    for event in batch:
        assert state.classify(event, CONFIG).disposition is EventDisposition.DUPLICATE  # type: ignore[arg-type]


def test_beyond_the_window_the_event_goes_to_late_not_the_bin() -> None:
    """Outside the window we cannot tell redelivery from new data. Sending it to the
    late topic is the honest choice: the sink's unique index prevents double
    counting, whereas discarding it could lose real data (CLAUDE.md S8)."""
    state = DedupState()
    oldest = at(1, 0)
    state.classify(oldest, CONFIG)  # type: ignore[arg-type]
    state.classify(at(REPLAY_WINDOW + 200, 5000), CONFIG)  # type: ignore[arg-type]

    decision = state.classify(oldest, CONFIG)  # type: ignore[arg-type]
    assert decision.disposition is EventDisposition.TOO_LATE
    assert decision.watermark.beyond_window == 1


def test_beyond_window_count_is_tracked_separately() -> None:
    """A rising count means reordering exceeds the window, which is a signal to
    widen it rather than something to discover during an incident."""
    state = DedupState()
    state.classify(at(1, 0), CONFIG)  # type: ignore[arg-type]
    state.classify(at(1000, 10_000), CONFIG)  # type: ignore[arg-type]
    state.classify(at(2, 10), CONFIG)  # type: ignore[arg-type]
    assert state.totals["beyond_window"] == 1


def test_a_large_forward_jump_resets_the_window() -> None:
    """Nothing older than the new mark can remain in range, so the mask must clear
    rather than carry stale bits that would falsely report duplicates."""
    watermark = VehicleWatermark.initial(at(1, 0))  # type: ignore[arg-type]
    decision = classify(at(1 + REPLAY_WINDOW * 3, 5000), watermark, CONFIG)  # type: ignore[arg-type]
    assert decision.watermark.seen_mask == 1


# --------------------------------------------------------------------------
# Genuine out-of-order data
# --------------------------------------------------------------------------


def test_new_out_of_order_event_within_lateness_is_late_not_duplicate() -> None:
    """It has never been seen, so it must not be discarded as a duplicate."""
    state = DedupState()
    state.classify(at(10, 600), CONFIG)  # type: ignore[arg-type]
    decision = state.classify(at(5, 500), CONFIG)  # type: ignore[arg-type]
    assert decision.disposition is EventDisposition.LATE


def test_new_out_of_order_event_beyond_lateness_is_too_late() -> None:
    state = DedupState()
    state.classify(at(10, 10_000), CONFIG)  # type: ignore[arg-type]
    decision = state.classify(at(5, 0), CONFIG)  # type: ignore[arg-type]
    assert decision.disposition is EventDisposition.TOO_LATE


def test_an_out_of_order_event_is_marked_seen_so_its_replay_is_a_duplicate() -> None:
    """Otherwise the reconciler would receive it twice."""
    state = DedupState()
    state.classify(at(10, 600), CONFIG)  # type: ignore[arg-type]
    assert state.classify(at(5, 500), CONFIG).disposition is EventDisposition.LATE  # type: ignore[arg-type]
    assert state.classify(at(5, 500), CONFIG).disposition is EventDisposition.DUPLICATE  # type: ignore[arg-type]


def test_lateness_is_decided_on_event_time_not_sequence_distance() -> None:
    """Sequence distance says nothing about how stale data is; only event time does."""
    state = DedupState()
    state.classify(at(1000, 600), CONFIG)  # type: ignore[arg-type]
    # Far behind in seq, but only 100s behind in time.
    decision = state.classify(at(990, 500), CONFIG)  # type: ignore[arg-type]
    assert decision.disposition is EventDisposition.LATE


# --------------------------------------------------------------------------
# Timestamp watermark
# --------------------------------------------------------------------------


def test_watermark_timestamp_never_moves_backwards() -> None:
    """A device with a briefly wrong clock must not be able to drag the timestamp
    watermark back and make subsequent good events look late."""
    watermark = VehicleWatermark.initial(at(10, 600))  # type: ignore[arg-type]
    decision = classify(at(11, 100), watermark, CONFIG)  # type: ignore[arg-type]
    assert decision.accepted
    assert decision.watermark.highest_ts == at(10, 600).ts  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# State management
# --------------------------------------------------------------------------


def test_state_tracks_each_vehicle_independently() -> None:
    state = DedupState()
    other = "5GZCZ43D13S812715"
    state.classify(at(10, 100), CONFIG)  # type: ignore[arg-type]
    state.classify(make_event(seq=1, offset_seconds=0, vin=other), CONFIG)
    assert state.vehicle_count == 2
    # A low seq on one vehicle is unaffected by a high seq on another.
    assert state.classify(make_event(seq=2, offset_seconds=10, vin=other), CONFIG).accepted


def test_loading_persisted_state_prevents_recounting_after_a_restart() -> None:
    """Without this, a restart re-accepts everything inside the retention window as
    new and double-counts it."""
    state = DedupState()
    watermark = VehicleWatermark.initial(at(50, 500))  # type: ignore[arg-type]
    state.load({watermark.vin: watermark})
    assert state.classify(at(50, 500), CONFIG).disposition is EventDisposition.DUPLICATE  # type: ignore[arg-type]


def test_totals_report_every_disposition() -> None:
    state = DedupState()
    state.classify(at(10, 600), CONFIG)  # type: ignore[arg-type]
    state.classify(at(10, 600), CONFIG)  # type: ignore[arg-type]
    state.classify(at(5, 500), CONFIG)  # type: ignore[arg-type]
    totals = state.totals
    assert totals["accepted"] == 1
    assert totals["duplicates"] == 1
    assert totals["late"] == 1


def test_negative_lateness_window_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        DedupConfig(allowed_lateness_seconds=-1)


def test_zero_lateness_window_sends_all_out_of_order_data_to_too_late() -> None:
    """A valid configuration: some deployments want no in-stream lateness at all."""
    strict = DedupConfig(allowed_lateness_seconds=0)
    state = DedupState()
    state.classify(at(10, 600), strict)  # type: ignore[arg-type]
    assert state.classify(at(5, 590), strict).disposition is EventDisposition.TOO_LATE  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------


@given(st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_an_event_is_never_accepted_twice(seqs: list[int]) -> None:
    """The core safety property. Accepting the same sequence number twice for one
    vehicle would double-count fuel."""
    state = DedupState()
    accepted: list[int] = []
    for seq in seqs:
        if state.classify(at(seq, seq * 10), CONFIG).accepted:  # type: ignore[arg-type]
            accepted.append(seq)
    assert len(accepted) == len(set(accepted))


@given(st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=120))
@settings(max_examples=250, deadline=None)
def test_property_every_event_gets_exactly_one_disposition(seqs: list[int]) -> None:
    """Nothing may vanish: every event is accepted, duplicated, late or too late."""
    state = DedupState()
    for seq in seqs:
        decision = state.classify(at(seq, seq * 10), CONFIG)  # type: ignore[arg-type]
        assert decision.disposition in set(EventDisposition)


@given(st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=100))
@settings(max_examples=200, deadline=None)
def test_property_replaying_the_whole_stream_accepts_nothing_new(
    seqs: list[int],
) -> None:
    """The at-least-once guarantee: a full redelivery must be inert."""
    state = DedupState()
    for seq in seqs:
        state.classify(at(seq, seq * 10), CONFIG)  # type: ignore[arg-type]

    for seq in seqs:
        assert not state.classify(at(seq, seq * 10), CONFIG).accepted  # type: ignore[arg-type]


@given(st.lists(st.integers(min_value=1, max_value=60), min_size=1, max_size=60))
@settings(max_examples=200, deadline=None)
def test_property_within_the_window_replay_is_exactly_duplicate(
    seqs: list[int],
) -> None:
    """Inside the window the answer is exact, never merely conservative: a replay is
    reported as a duplicate, not as late data for the reconciler to re-handle."""
    state = DedupState()
    for seq in seqs:
        state.classify(at(seq, seq * 10), CONFIG)  # type: ignore[arg-type]

    for seq in set(seqs):
        decision = state.classify(at(seq, seq * 10), CONFIG)  # type: ignore[arg-type]
        assert decision.disposition is EventDisposition.DUPLICATE


@given(st.integers(min_value=1, max_value=10_000), st.integers(min_value=1, max_value=200))
@settings(max_examples=300)
def test_property_the_highest_sequence_is_always_marked_seen(start: int, advance: int) -> None:
    """Bit 0 must always be set, or the newest event would be re-accepted."""
    watermark = VehicleWatermark.initial(at(start, 0))  # type: ignore[arg-type]
    advanced = classify(at(start + advance, 100), watermark, CONFIG).watermark  # type: ignore[arg-type]
    assert advanced.has_seen(advanced.highest_seq)


@given(st.integers(min_value=0, max_value=REPLAY_WINDOW * 2))
@settings(max_examples=200)
def test_property_has_seen_never_looks_outside_the_window(offset: int) -> None:
    watermark = VehicleWatermark.initial(at(1000, 0))  # type: ignore[arg-type]
    seq = 1000 - offset
    if offset >= REPLAY_WINDOW:
        assert not watermark.has_seen(seq)


def test_window_width_is_generous_relative_to_sampling() -> None:
    """At a 10 s sample interval, 64 sequence numbers is over ten minutes of one
    vehicle's history - far more than any realistic reordering burst."""
    assert REPLAY_WINDOW * timedelta(seconds=10) > timedelta(minutes=10)
