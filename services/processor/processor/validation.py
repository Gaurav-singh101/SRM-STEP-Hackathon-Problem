"""Event decoding and validation at the ingest edge.

Every malformed event gets a machine-readable reason code and goes to the DLQ. The
consumer never crashes on bad input and never silently drops it (CLAUDE.md S8) -
both failure modes are worse than a DLQ, because one halts the pipeline and the
other loses data while appearing healthy.

Pure: takes bytes, returns a verdict. No I/O, so every rejection path is unit
testable without a broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import orjson
from pydantic import ValidationError

from fleet_common.events import TelemetryEvent

#: Cap on a stored DLQ payload. A pathological message must not be able to blow up
#: the DLQ topic or the operator's console; the reason code is what matters for
#: triage, and the truncation is recorded.
MAX_DLQ_PAYLOAD_BYTES: Final[int] = 4096


class RejectReason(StrEnum):
    """Machine-readable DLQ reason codes.

    Enumerated rather than free text so the DLQ can be aggregated: "how many events
    failed and why" is the first question during an incident, and string matching on
    prose does not answer it.
    """

    NOT_JSON = "not_json"
    NOT_AN_OBJECT = "not_an_object"
    SCHEMA_INVALID = "schema_invalid"
    INVALID_VIN = "invalid_vin"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    NAIVE_TIMESTAMP = "naive_timestamp"
    TIMESTAMP_TOO_FAR_FUTURE = "timestamp_too_far_future"
    TENANT_MISMATCH = "tenant_mismatch"
    EMPTY_PAYLOAD = "empty_payload"


@dataclass(frozen=True, slots=True)
class Rejection:
    """One event bound for the DLQ."""

    reason: RejectReason
    detail: str
    payload: bytes
    #: Best-effort identity, for triage. Absent when the payload was unparseable.
    vin: str | None = None
    tenant_id: str | None = None

    @property
    def truncated_payload(self) -> bytes:
        return self.payload[:MAX_DLQ_PAYLOAD_BYTES]

    @property
    def was_truncated(self) -> bool:
        return len(self.payload) > MAX_DLQ_PAYLOAD_BYTES


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """Bounds applied beyond the schema itself."""

    #: An event timestamped far in the future is a broken device clock, not data.
    #: Accepting it would poison every window and rollup it touched, and unlike a
    #: late event it cannot be reconciled later.
    max_future_skew_seconds: int = 300
    #: When set, events claiming another tenant are rejected. On a real ingest path
    #: the tenant is device-bound; this is the enforcement point.
    expected_tenant_id: str | None = None


def decode(raw: bytes, config: ValidationConfig, *, now: datetime) -> TelemetryEvent | Rejection:
    """Decode and validate one message.

    ``now`` is injected rather than read from the clock, so future-skew rejection is
    deterministic and testable (CLAUDE.md S12).
    """
    if not raw:
        return Rejection(RejectReason.EMPTY_PAYLOAD, "message body was empty", raw)

    try:
        parsed: Any = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        return Rejection(RejectReason.NOT_JSON, str(exc), raw)

    if not isinstance(parsed, dict):
        return Rejection(
            RejectReason.NOT_AN_OBJECT,
            f"expected a JSON object, got {type(parsed).__name__}",
            raw,
        )

    # Pulled before validation so a rejection still carries identity for triage.
    vin = parsed.get("vin") if isinstance(parsed.get("vin"), str) else None
    tenant_id = parsed.get("tenant_id") if isinstance(parsed.get("tenant_id"), str) else None

    try:
        event = TelemetryEvent.model_validate(parsed)
    except ValidationError as exc:
        return Rejection(_classify(exc), _summarise(exc), raw, vin=vin, tenant_id=tenant_id)

    if config.expected_tenant_id is not None and event.tenant_id != config.expected_tenant_id:
        return Rejection(
            RejectReason.TENANT_MISMATCH,
            f"event claims tenant {event.tenant_id!r}",
            raw,
            vin=vin,
            tenant_id=tenant_id,
        )

    skew = event.ts - now
    if skew > timedelta(seconds=config.max_future_skew_seconds):
        return Rejection(
            RejectReason.TIMESTAMP_TOO_FAR_FUTURE,
            f"event time is {skew.total_seconds():.0f}s ahead of now",
            raw,
            vin=vin,
            tenant_id=tenant_id,
        )

    return event


def _classify(exc: ValidationError) -> RejectReason:
    """Map a Pydantic failure to a specific reason code where we can.

    Specific codes matter operationally: a flood of `invalid_vin` points at one
    device or one OEM integration, while `schema_invalid` points at a contract
    change. Collapsing both into one code would hide that difference.
    """
    for error in exc.errors():
        message = str(error.get("msg", "")).lower()
        field = error.get("loc", ())
        if "invalid vin" in message:
            return RejectReason.INVALID_VIN
        if "unsupported schema_version" in message:
            return RejectReason.UNSUPPORTED_SCHEMA_VERSION
        if "timezone-aware" in message:
            return RejectReason.NAIVE_TIMESTAMP
        if field and field[0] == "vin":
            return RejectReason.INVALID_VIN
    return RejectReason.SCHEMA_INVALID


def _summarise(exc: ValidationError) -> str:
    """A compact, log-safe summary of why validation failed.

    Field names and messages only - never the field values. A value could be a
    precise location, which must not reach logs (CLAUDE.md S11).
    """
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(p) for p in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    remaining = len(exc.errors()) - 5
    if remaining > 0:
        parts.append(f"(+{remaining} more)")
    return "; ".join(parts)
