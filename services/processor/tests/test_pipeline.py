"""Tests for the processing pipeline.

Pure, so they run in milliseconds against no infrastructure. The most important one
is ``test_idle_segment_spanning_two_batches_is_not_cut_at_the_boundary``: batch
boundaries are an artefact of polling and must be invisible to the business logic. A
processor that reset per batch would silently under-report every long idle - which
is to say, exactly the expensive ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import orjson
import pytest

from fleet_common.domain.fuel import FuelPrice, VehicleClass
from fleet_common.domain.idle import IdleConfig
from fleet_common.events import Ignition, Powertrain
from fleet_common.testing import TEST_TENANT, TEST_VIN, make_event, make_idle_run
from processor.dedup import DedupConfig
from processor.pipeline import (
    AlertType,
    BatchProcessor,
    PipelineConfig,
    VehicleRef,
)
from processor.validation import RejectReason, ValidationConfig

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)

PRICE = FuelPrice(price_id="inr-diesel-2026-09", price_per_litre=Decimal("92.50"), currency="INR")

DIESEL_TRUCK = VehicleRef(
    vin=TEST_VIN,
    tenant_id=TEST_TENANT,
    powertrain=Powertrain.DIESEL,
    vehicle_class=VehicleClass.MEDIUM_TRUCK,
    fleet_id=1,
)

BEV_CAR = VehicleRef(
    vin=TEST_VIN,
    tenant_id=TEST_TENANT,
    powertrain=Powertrain.BEV,
    vehicle_class=VehicleClass.CAR,
    fleet_id=1,
)

VEHICLES = {TEST_VIN: DIESEL_TRUCK}


def config(**overrides: object) -> PipelineConfig:
    base = {
        "idle": IdleConfig(
            min_seconds=180,
            entry_speed_kph=2.0,
            exit_speed_kph=5.0,
            excessive_seconds=600,
            gap_tolerance_seconds=30,
        ),
        "dedup": DedupConfig(allowed_lateness_seconds=300),
        "validation": ValidationConfig(max_future_skew_seconds=300),
        "prices": {"diesel": PRICE, "petrol": PRICE},
    }
    base.update(overrides)
    return PipelineConfig(**base)  # type: ignore[arg-type]


def wire(*events: object) -> list[bytes]:
    """Serialise events exactly as the producer does."""
    return [orjson.dumps(e.model_dump(mode="json")) for e in events]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# The batch-boundary test
# --------------------------------------------------------------------------


def test_idle_segment_spanning_two_batches_is_not_cut_at_the_boundary() -> None:
    """Batch boundaries are a polling artefact and must not appear in the output."""
    processor = BatchProcessor(config())
    events = make_idle_run(start=0, duration=600, step=10)

    first = processor.process(wire(*events[:20]), VEHICLES, now=NOW)
    second = processor.process(wire(*events[20:]), VEHICLES, now=NOW)

    # Nothing closes yet: the vehicle is still idling at the end of batch two.
    assert first.idle_segments == []
    assert second.idle_segments == []

    # A moving sample closes it, and the duration spans the whole run.
    moving = make_event(offset_seconds=610, speed_kph=40.0, seq=999)
    third = processor.process(wire(moving), VEHICLES, now=NOW)

    assert len(third.idle_segments) == 1
    assert third.idle_segments[0].segment.duration_seconds == pytest.approx(610.0)


def test_dedup_watermark_survives_across_batches() -> None:
    """A redelivered batch must cost nothing, which needs state to persist."""
    processor = BatchProcessor(config())
    events = make_idle_run(start=0, duration=200, step=10)

    first = processor.process(wire(*events), VEHICLES, now=NOW)
    replay = processor.process(wire(*events), VEHICLES, now=NOW)

    assert len(first.accepted) == len(events)
    assert replay.accepted == []
    assert replay.duplicates == len(events)


# --------------------------------------------------------------------------
# Validation and the DLQ
# --------------------------------------------------------------------------


def test_valid_event_is_accepted() -> None:
    result = BatchProcessor(config()).process(
        wire(make_event(offset_seconds=0, speed_kph=30.0)), VEHICLES, now=NOW
    )
    assert len(result.accepted) == 1
    assert result.rejections == []


def test_non_json_goes_to_the_dlq() -> None:
    result = BatchProcessor(config()).process([b"{not json"], VEHICLES, now=NOW)
    assert len(result.rejections) == 1
    assert result.rejections[0].reason is RejectReason.NOT_JSON


def test_empty_payload_goes_to_the_dlq() -> None:
    result = BatchProcessor(config()).process([b""], VEHICLES, now=NOW)
    assert result.rejections[0].reason is RejectReason.EMPTY_PAYLOAD


def test_json_array_goes_to_the_dlq() -> None:
    result = BatchProcessor(config()).process([b"[1,2,3]"], VEHICLES, now=NOW)
    assert result.rejections[0].reason is RejectReason.NOT_AN_OBJECT


def test_impossible_speed_goes_to_the_dlq() -> None:
    """The simulator injects these deliberately (PDF S7)."""
    payload = orjson.loads(wire(make_event(offset_seconds=0))[0])
    payload["speed_kph"] = 999.0
    result = BatchProcessor(config()).process([orjson.dumps(payload)], VEHICLES, now=NOW)
    assert result.rejections[0].reason is RejectReason.SCHEMA_INVALID


def test_invalid_vin_gets_its_own_reason_code() -> None:
    """A flood of invalid_vin points at one device; schema_invalid points at a
    contract change. Collapsing them would hide that difference."""
    payload = orjson.loads(wire(make_event(offset_seconds=0))[0])
    payload["vin"] = "1HGCM82634A004352"  # well-formed, wrong check digit
    result = BatchProcessor(config()).process([orjson.dumps(payload)], VEHICLES, now=NOW)
    assert result.rejections[0].reason is RejectReason.INVALID_VIN


def test_unsupported_schema_version_gets_its_own_reason_code() -> None:
    payload = orjson.loads(wire(make_event(offset_seconds=0))[0])
    payload["schema_version"] = 99
    result = BatchProcessor(config()).process([orjson.dumps(payload)], VEHICLES, now=NOW)
    assert result.rejections[0].reason is RejectReason.UNSUPPORTED_SCHEMA_VERSION


def test_far_future_timestamp_goes_to_the_dlq() -> None:
    """A broken device clock would poison every window it touched, and unlike a late
    event it cannot be reconciled afterwards."""
    future = make_event(offset_seconds=0, ts=NOW + timedelta(hours=2))
    result = BatchProcessor(config()).process(wire(future), VEHICLES, now=NOW)
    assert result.rejections[0].reason is RejectReason.TIMESTAMP_TOO_FAR_FUTURE


def test_small_clock_skew_is_tolerated() -> None:
    """Device clocks drift by seconds routinely; rejecting that would be useless."""
    slightly_ahead = make_event(offset_seconds=0, ts=NOW + timedelta(seconds=60))
    result = BatchProcessor(config()).process(wire(slightly_ahead), VEHICLES, now=NOW)
    assert result.rejections == []
    assert len(result.accepted) == 1


def test_tenant_mismatch_is_rejected_when_the_tenant_is_pinned() -> None:
    """On a real ingest path the tenant is device-bound, never taken from the
    payload (CLAUDE.md S3). This is the enforcement point."""
    processor = BatchProcessor(
        config(validation=ValidationConfig(expected_tenant_id="tenant-real"))
    )
    result = processor.process(wire(make_event(offset_seconds=0)), VEHICLES, now=NOW)
    assert result.rejections[0].reason is RejectReason.TENANT_MISMATCH


def test_one_bad_event_does_not_discard_the_rest_of_the_batch() -> None:
    """The consumer must never crash or abandon a batch on bad input
    (CLAUDE.md S8)."""
    good = wire(*make_idle_run(start=0, duration=100, step=10))
    messages = [*good[:5], b"{{{garbage", *good[5:]]
    result = BatchProcessor(config()).process(messages, VEHICLES, now=NOW)
    assert len(result.accepted) == len(good)
    assert len(result.rejections) == 1


def test_rejection_payload_is_capped() -> None:
    """A pathological message must not be able to blow up the DLQ topic."""
    huge = b'{"vin": "' + b"A" * 100_000 + b'"}'
    result = BatchProcessor(config()).process([huge], VEHICLES, now=NOW)
    rejection = result.rejections[0]
    assert rejection.was_truncated
    assert len(rejection.truncated_payload) == 4096


def test_dlq_rate_is_reported() -> None:
    good = wire(*make_idle_run(start=0, duration=100, step=10))
    result = BatchProcessor(config()).process([*good, b"bad"], VEHICLES, now=NOW)
    assert 0.0 < result.dlq_rate < 1.0


# --------------------------------------------------------------------------
# Lateness
# --------------------------------------------------------------------------


def test_out_of_order_event_within_the_window_is_routed_to_late() -> None:
    processor = BatchProcessor(config())
    processor.process(
        wire(make_event(offset_seconds=600, speed_kph=0.0, seq=100)), VEHICLES, now=NOW
    )
    result = processor.process(
        wire(make_event(offset_seconds=500, speed_kph=0.0, seq=50)), VEHICLES, now=NOW
    )
    assert len(result.late) == 1
    assert result.accepted == []


def test_event_beyond_the_lateness_window_is_still_not_dropped() -> None:
    """CLAUDE.md S8: never silently drop. "Too late for the stream" is not "may be
    thrown away" - the batch reconciler still needs it."""
    processor = BatchProcessor(config())
    processor.process(
        wire(make_event(offset_seconds=10_000, speed_kph=0.0, seq=100)),
        VEHICLES,
        now=NOW,
    )
    result = processor.process(
        wire(make_event(offset_seconds=0, speed_kph=0.0, seq=1)), VEHICLES, now=NOW
    )
    assert len(result.late) == 1


def test_a_stale_device_clock_cannot_drag_the_watermark_backwards() -> None:
    """seq is authoritative for ordering. If a later-seq event carried an older
    timestamp and moved the watermark back, subsequent good events would start
    looking late."""
    processor = BatchProcessor(config())
    processor.process(
        wire(make_event(offset_seconds=600, speed_kph=0.0, seq=10)), VEHICLES, now=NOW
    )
    # Higher seq but an older timestamp: accepted on seq, watermark ts must hold.
    processor.process(
        wire(make_event(offset_seconds=100, speed_kph=0.0, seq=11)), VEHICLES, now=NOW
    )
    watermark = processor.dedup_state.watermarks[TEST_VIN]
    assert watermark.highest_ts == make_event(offset_seconds=600).ts


# --------------------------------------------------------------------------
# Idle detection and pricing
# --------------------------------------------------------------------------


def test_closed_segment_is_priced_with_its_assumptions() -> None:
    processor = BatchProcessor(config())
    events = [
        *make_idle_run(start=0, duration=600, step=10),
        make_event(offset_seconds=610, speed_kph=40.0, seq=500),
    ]
    result = processor.process(wire(*events), VEHICLES, now=NOW)

    assert len(result.idle_segments) == 1
    priced = result.idle_segments[0]
    assert priced.estimate is not None
    assert priced.estimate.burn_rate_lph == 2.4
    assert priced.estimate.price_id == "inr-diesel-2026-09"
    assert priced.estimate.idle_threshold_seconds == 180
    assert priced.cost is not None and priced.cost > 0


def test_sub_threshold_idle_produces_no_segment() -> None:
    processor = BatchProcessor(config())
    events = [
        *make_idle_run(start=0, duration=120, step=10),
        make_event(offset_seconds=130, speed_kph=40.0, seq=500),
    ]
    result = processor.process(wire(*events), VEHICLES, now=NOW)
    assert result.idle_segments == []


def test_bev_segment_is_excluded_from_cost_not_zeroed() -> None:
    """A zero would be indistinguishable from a measured zero and would flow into a
    fleet total as though it were one (D9)."""
    processor = BatchProcessor(config())
    events = [
        *make_idle_run(start=0, duration=600, step=10),
        make_event(offset_seconds=610, speed_kph=40.0, seq=500),
    ]
    result = processor.process(wire(*events), {TEST_VIN: BEV_CAR}, now=NOW)

    assert len(result.idle_segments) == 1
    priced = result.idle_segments[0]
    assert priced.bev_excluded is True
    assert priced.estimate is None
    assert priced.cost is None


def test_missing_price_records_the_segment_without_a_cost() -> None:
    """A missing price is a configuration problem. Inventing a number would produce
    something indistinguishable from a real figure."""
    processor = BatchProcessor(config(prices={}))
    events = [
        *make_idle_run(start=0, duration=600, step=10),
        make_event(offset_seconds=610, speed_kph=40.0, seq=500),
    ]
    result = processor.process(wire(*events), VEHICLES, now=NOW)
    assert len(result.idle_segments) == 1
    assert result.idle_segments[0].estimate is None
    assert result.idle_segments[0].bev_excluded is False


def test_unconfigured_burn_rate_records_the_segment_without_a_cost() -> None:
    """ADR-0002 leaves hybrid trucks unconfigured on purpose."""
    hybrid_truck = VehicleRef(
        vin=TEST_VIN,
        tenant_id=TEST_TENANT,
        powertrain=Powertrain.HYBRID,
        vehicle_class=VehicleClass.HEAVY_TRUCK,
        fleet_id=1,
    )
    processor = BatchProcessor(config())
    events = [
        *make_idle_run(start=0, duration=600, step=10),
        make_event(offset_seconds=610, speed_kph=40.0, seq=500),
    ]
    result = processor.process(wire(*events), {TEST_VIN: hybrid_truck}, now=NOW)
    assert result.idle_segments[0].estimate is None


def test_unknown_vehicle_is_counted_not_an_error() -> None:
    """The seed job may not have run yet. Counted so "why is cost missing" has an
    answer rather than being a mystery."""
    processor = BatchProcessor(config())
    result = processor.process(wire(*make_idle_run(start=0, duration=200, step=10)), {}, now=NOW)
    assert result.unknown_vehicle > 0
    assert result.idle_segments == []
    # Events are still accepted and stored: telemetry is not lost because the
    # vehicle master is incomplete.
    assert len(result.accepted) > 0


# --------------------------------------------------------------------------
# Alerts (D10)
# --------------------------------------------------------------------------


def test_excessive_idle_raises_exactly_one_alert() -> None:
    """Without once-per-segment semantics a long idle storms alerts."""
    processor = BatchProcessor(config())
    result = processor.process(
        wire(*make_idle_run(start=0, duration=1800, step=10)), VEHICLES, now=NOW
    )
    excessive = [a for a in result.alerts if a.alert_type is AlertType.EXCESSIVE_IDLE_IN_PROGRESS]
    assert len(excessive) == 1


def test_alert_carries_the_timestamp_the_slo_is_measured_from() -> None:
    """D10 measures latency as persisted_at - triggering_event_ts."""
    processor = BatchProcessor(config())
    result = processor.process(
        wire(*make_idle_run(start=0, duration=900, step=10)), VEHICLES, now=NOW
    )
    alert = result.alerts[0]
    assert alert.triggering_event_ts is not None
    assert alert.idle_segment_started_at is not None
    assert alert.vin == TEST_VIN
    assert alert.tenant_id == TEST_TENANT


def test_alert_fires_while_the_vehicle_is_still_idling() -> None:
    """The alert must be actionable: it exists so someone can call the driver now,
    which means it cannot wait for the segment to close."""
    processor = BatchProcessor(config())
    result = processor.process(
        wire(*make_idle_run(start=0, duration=700, step=10)), VEHICLES, now=NOW
    )
    assert result.alerts, "no alert raised while idling"
    assert result.idle_segments == [], "segment closed, so the alert was not in-progress"


def test_short_idle_raises_no_alert() -> None:
    processor = BatchProcessor(config())
    result = processor.process(
        wire(*make_idle_run(start=0, duration=300, step=10)), VEHICLES, now=NOW
    )
    assert result.alerts == []


# --------------------------------------------------------------------------
# Latest state (Redis)
# --------------------------------------------------------------------------


def test_latest_state_reflects_the_newest_event() -> None:
    processor = BatchProcessor(config())
    events = [
        make_event(offset_seconds=0, speed_kph=0.0, seq=1),
        make_event(offset_seconds=10, speed_kph=55.0, seq=2, odometer_km=1234.5),
    ]
    result = processor.process(wire(*events), VEHICLES, now=NOW)
    state = result.latest_state[TEST_VIN]
    assert state.speed_kph == pytest.approx(55.0)
    assert state.odometer_km == pytest.approx(1234.5)
    assert state.ignition == Ignition.ON.value


def test_latest_state_exposes_in_progress_idle() -> None:
    """Lets the dashboard show a vehicle idling right now, before any segment
    closes."""
    processor = BatchProcessor(config())
    result = processor.process(
        wire(*make_idle_run(start=0, duration=400, step=10)), VEHICLES, now=NOW
    )
    state = result.latest_state[TEST_VIN]
    assert state.open_idle_seconds == pytest.approx(400.0)
    assert state.idle_state == "confirmed"


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_flush_closes_segments_open_at_shutdown() -> None:
    """The long segments - the expensive ones - are the most likely to be open."""
    processor = BatchProcessor(config())
    processor.process(wire(*make_idle_run(start=0, duration=900, step=10)), VEHICLES, now=NOW)
    flushed = processor.flush_open_segments(VEHICLES)
    assert len(flushed) == 1
    assert flushed[0].segment.duration_seconds == pytest.approx(900.0)
    assert flushed[0].estimate is not None


def test_flush_is_idempotent() -> None:
    processor = BatchProcessor(config())
    processor.process(wire(*make_idle_run(start=0, duration=900, step=10)), VEHICLES, now=NOW)
    assert len(processor.flush_open_segments(VEHICLES)) == 1
    assert processor.flush_open_segments(VEHICLES) == []


def test_drop_vehicles_releases_state_after_a_rebalance() -> None:
    """Otherwise memory grows with rebalance count instead of staying bounded by
    assigned partitions (CLAUDE.md S9)."""
    processor = BatchProcessor(config())
    processor.process(wire(*make_idle_run(start=0, duration=200, step=10)), VEHICLES, now=NOW)
    assert processor.tracked_vehicles == 1
    assert TEST_VIN in processor.dedup_state.watermarks

    processor.drop_vehicles([TEST_VIN])
    assert processor.tracked_vehicles == 0
    assert TEST_VIN not in processor.dedup_state.watermarks


def test_empty_batch_is_handled() -> None:
    result = BatchProcessor(config()).process([], VEHICLES, now=NOW)
    assert result.total_consumed == 0
    assert result.dlq_rate == 0.0


def test_counts_account_for_every_consumed_message() -> None:
    """Nothing may vanish silently: consumed must equal accepted + rejected + late
    + duplicates, or the pipeline is losing events without saying so."""
    processor = BatchProcessor(config())
    good = wire(*make_idle_run(start=0, duration=200, step=10))
    messages = [*good, *good, b"garbage"]
    result = processor.process(messages, VEHICLES, now=NOW)
    assert result.total_consumed == len(messages)
