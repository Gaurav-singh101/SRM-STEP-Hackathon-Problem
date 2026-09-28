"""Contract tests: the Pydantic model and ``schemas/telemetry.raw.v1.json`` agree.

This is the agreed substitute for Pact on the processor-to-API relationship (G9).
Pact models an HTTP consumer and provider; the processor never calls the API, it
writes to shared stores, so there is no such pair to contract-test. What actually
needs protecting is the *event contract*, and these tests do that directly: both
sides import the same schema file, and a field added to one without the other
fails here rather than in production.

The schema file is the single source of truth (CLAUDE.md S7). When these tests
disagree, the schema wins and the model is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from fleet_common.events import (
    SCHEMA_VERSION,
    DiscreteEvent,
    EngineState,
    Ignition,
    TelemetryEvent,
)
from fleet_common.testing import TEST_TENANT, TEST_VIN

#: Walk up from this test file to the repository root, then into schemas/.
#: Resolved from __file__ so the test works regardless of the working directory.
REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "telemetry.raw.v1.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    assert SCHEMA_PATH.is_file(), f"event schema not found at {SCHEMA_PATH}"
    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        loaded: dict[str, Any] = json.load(fh)
    return loaded


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def canonical_event() -> TelemetryEvent:
    """A fully populated event: every optional field set, so nothing is untested."""
    return TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id="018f3a4b-0000-7000-8000-000000000001",
        tenant_id=TEST_TENANT,
        device_id="device-001",
        vin=TEST_VIN,
        seq=88412,
        ts="2026-09-25T10:15:02.120Z",  # type: ignore[arg-type]
        speed_kph=64.2,
        odometer_km=18234.7,
        heading_deg=182.5,
        ignition=Ignition.ON,
        engine_state=EngineState.RUNNING,
        rpm=1450,
        engine_hours=4821.5,
        fuel_level_pct=41.0,
        fuel_rate_lph=7.3,
        soc_pct=None,
        lat=21.1702,
        lon=72.8311,
        gps_valid=True,
        hdop=0.9,
        dtc=("P0301",),
        evt=DiscreteEvent.HARSH_BRAKE,
        pto_active=False,
    )


def as_wire_json(event: TelemetryEvent) -> dict[str, Any]:
    """Serialise exactly as the producer puts it on the wire."""
    payload: dict[str, Any] = json.loads(event.model_dump_json())
    return payload


# --------------------------------------------------------------------------
# Structural agreement between model and schema
# --------------------------------------------------------------------------


def test_schema_itself_is_valid(validator: Draft202012Validator) -> None:
    assert validator is not None


def test_model_and_schema_declare_the_same_fields(schema: dict[str, Any]) -> None:
    """A field added on one side only is the exact drift this test exists to catch."""
    schema_fields = set(schema["properties"])
    model_fields = set(TelemetryEvent.model_fields)
    assert model_fields == schema_fields, (
        f"only in model: {sorted(model_fields - schema_fields)}; "
        f"only in schema: {sorted(schema_fields - model_fields)}"
    )


def test_required_fields_match(schema: dict[str, Any]) -> None:
    """The model's mandatory fields must be exactly the schema's required list."""
    schema_required = set(schema["required"])
    model_required = {name for name, f in TelemetryEvent.model_fields.items() if f.is_required()}
    # schema_version carries a default in the model but is required on the wire,
    # because a consumer must never have to guess which contract it is reading.
    assert model_required | {"schema_version"} == schema_required


def test_schema_forbids_unknown_fields(schema: dict[str, Any]) -> None:
    """Both sides reject extras, so a typo'd field cannot be silently dropped."""
    assert schema["additionalProperties"] is False


def test_model_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate({**as_wire_json(canonical_event()), "surprise": 1})


@pytest.mark.parametrize(
    ("field", "enum_type"),
    [("ignition", Ignition), ("engine_state", EngineState), ("evt", DiscreteEvent)],
)
def test_enum_values_match_the_schema(schema: dict[str, Any], field: str, enum_type: type) -> None:
    schema_values = {v for v in schema["properties"][field]["enum"] if v is not None}
    assert schema_values == {member.value for member in enum_type}


@pytest.mark.parametrize(
    ("field", "keyword", "expected"),
    [
        ("speed_kph", "maximum", 300),
        ("odometer_km", "maximum", 5_000_000),
        ("fuel_level_pct", "maximum", 100),
        ("rpm", "maximum", 10_000),
        ("lat", "minimum", -90),
        ("lon", "minimum", -180),
    ],
)
def test_numeric_bounds_match(
    schema: dict[str, Any], field: str, keyword: str, expected: float
) -> None:
    """Bounds are duplicated by necessity; this asserts they stay identical."""
    assert schema["properties"][field][keyword] == expected


def test_vin_pattern_excludes_confusable_letters(schema: dict[str, Any]) -> None:
    assert schema["properties"]["vin"]["pattern"] == "^[A-HJ-NPR-Z0-9]{17}$"


# --------------------------------------------------------------------------
# Round-trip: the producer's bytes satisfy the schema and reparse identically
# --------------------------------------------------------------------------


def test_serialised_event_validates_against_the_schema(
    validator: Draft202012Validator,
) -> None:
    """The decisive test: what we actually put on the wire is contract-valid."""
    errors = sorted(validator.iter_errors(as_wire_json(canonical_event())), key=str)
    assert errors == [], [e.message for e in errors]


def test_minimal_event_validates_against_the_schema(
    validator: Draft202012Validator,
) -> None:
    """Only the required fields: optional signals are genuinely optional."""
    minimal = TelemetryEvent(
        event_id="018f3a4b-0000-7000-8000-000000000002",
        tenant_id=TEST_TENANT,
        vin=TEST_VIN,
        seq=1,
        ts="2026-09-25T10:15:02.120Z",  # type: ignore[arg-type]
        speed_kph=0.0,
        odometer_km=1.0,
        ignition=Ignition.OFF,
    )
    errors = list(validator.iter_errors(as_wire_json(minimal)))
    assert errors == [], [e.message for e in errors]


def test_round_trip_is_lossless() -> None:
    """Serialise, reparse, and the event must be identical.

    At-least-once delivery means events are replayed and re-read constantly; a
    lossy round trip would corrupt aggregates in ways no unit test would show.
    """
    original = canonical_event()
    reparsed = TelemetryEvent.model_validate(as_wire_json(original))
    assert reparsed == original


def test_timestamp_serialises_as_utc_iso8601() -> None:
    """Event time must be unambiguous on the wire."""
    wire = as_wire_json(canonical_event())
    assert wire["ts"].startswith("2026-09-25T10:15:02.120")
    assert wire["ts"].endswith(("Z", "+00:00"))


# --------------------------------------------------------------------------
# Rejections: bad events must fail on BOTH sides, not just one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("speed_kph", 500.0),
        ("speed_kph", -1.0),
        ("fuel_level_pct", 101.0),
        ("lat", 91.0),
        ("lon", -181.0),
        ("rpm", 20_000),
        ("seq", -1),
        ("heading_deg", 360.0),
    ],
)
def test_out_of_range_values_are_rejected_by_both_sides(
    validator: Draft202012Validator, field: str, bad_value: float
) -> None:
    """Agreement on rejection matters as much as agreement on acceptance: a value
    only one side refuses would reach a store the other side thought was safe."""
    payload = as_wire_json(canonical_event())
    payload[field] = bad_value

    assert list(validator.iter_errors(payload)), f"schema accepted bad {field}={bad_value}"
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(payload)


def test_invalid_vin_is_rejected_by_the_model(validator: Draft202012Validator) -> None:
    """The check digit is not expressible in JSON Schema, so the model owns it.

    The schema catches shape, the model catches the checksum; together they cover
    what neither can alone.
    """
    payload = as_wire_json(canonical_event())
    payload["vin"] = "1HGCM82634A004352"  # well-formed, wrong check digit
    assert list(validator.iter_errors(payload)) == [], "shape is valid, so schema passes"
    with pytest.raises(ValidationError, match="invalid VIN"):
        TelemetryEvent.model_validate(payload)


def test_confusable_letter_in_vin_is_rejected_by_both_sides(
    validator: Draft202012Validator,
) -> None:
    payload = as_wire_json(canonical_event())
    payload["vin"] = "I" + TEST_VIN[1:]
    assert list(validator.iter_errors(payload))
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(payload)


def test_unknown_schema_version_is_rejected() -> None:
    """A consumer must refuse a contract it does not understand."""
    payload = as_wire_json(canonical_event())
    payload["schema_version"] = 99
    with pytest.raises(ValidationError, match="unsupported schema_version"):
        TelemetryEvent.model_validate(payload)


def test_naive_timestamp_is_rejected() -> None:
    """A naive timestamp silently adopts the reader's timezone, corrupting every
    duration and window boundary downstream."""
    payload = as_wire_json(canonical_event())
    payload["ts"] = "2026-09-25T10:15:02.120"
    with pytest.raises(ValidationError, match="timezone-aware"):
        TelemetryEvent.model_validate(payload)


def test_bad_dtc_pattern_is_rejected_by_the_schema(
    validator: Draft202012Validator,
) -> None:
    payload = as_wire_json(canonical_event())
    payload["dtc"] = ["NOTACODE"]
    assert list(validator.iter_errors(payload))
