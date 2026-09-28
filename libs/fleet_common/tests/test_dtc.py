"""Tests for OBD-II DTC parsing (hackathon PDF S9)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.dtc import DtcSystem, parse_dtc, parse_dtc_list


def test_parses_the_pdf_example_code() -> None:
    """P0301 appears in the hackathon PDF S8 illustrative event."""
    dtc = parse_dtc("P0301")
    assert dtc is not None
    assert dtc.system is DtcSystem.POWERTRAIN
    assert dtc.is_generic is True
    assert dtc.subsystem == "ignition_or_misfire"
    assert dtc.fault_number == "01"


@pytest.mark.parametrize(
    ("code", "system"),
    [
        ("P0420", DtcSystem.POWERTRAIN),
        ("B1234", DtcSystem.BODY),
        ("C0035", DtcSystem.CHASSIS),
        ("U0100", DtcSystem.NETWORK),
    ],
)
def test_recognises_each_system_letter(code: str, system: DtcSystem) -> None:
    dtc = parse_dtc(code)
    assert dtc is not None
    assert dtc.system is system


@pytest.mark.parametrize(
    ("code", "generic"), [("P0301", True), ("P1301", False), ("P2301", True), ("P3301", False)]
)
def test_distinguishes_generic_from_manufacturer_specific(code: str, generic: bool) -> None:
    """0 and 2 are SAE-defined; 1 and 3 are manufacturer-specific."""
    dtc = parse_dtc(code)
    assert dtc is not None
    assert dtc.is_generic is generic


def test_identifies_the_idle_control_family() -> None:
    """These are the codes a finance user can act on beside an idle cost figure."""
    dtc = parse_dtc("P0505")
    assert dtc is not None
    assert dtc.subsystem == "speed_idle_control_and_inputs"
    assert dtc.is_idle_related is True


def test_non_idle_code_is_not_flagged_idle_related() -> None:
    dtc = parse_dtc("P0301")
    assert dtc is not None
    assert dtc.is_idle_related is False


def test_non_powertrain_code_has_no_subsystem() -> None:
    """The subsystem table is powertrain-specific (SAE J2012)."""
    dtc = parse_dtc("B1234")
    assert dtc is not None
    assert dtc.subsystem is None
    assert dtc.is_idle_related is False


def test_hybrid_propulsion_subsystem_is_recognised() -> None:
    dtc = parse_dtc("P0A80")
    assert dtc is not None
    assert dtc.subsystem == "hybrid_propulsion"


def test_input_is_normalised_before_parsing() -> None:
    dtc = parse_dtc("  p0301  ")
    assert dtc is not None
    assert dtc.raw == "P0301"


@pytest.mark.parametrize(
    "code",
    ["", "P030", "P03011", "X0301", "P4301", "P03G1", "not-a-code", "0301"],
)
def test_malformed_codes_return_none(code: str) -> None:
    """None, not an exception: one bad code must not fail the whole event."""
    assert parse_dtc(code) is None


def test_parse_list_drops_unparseable_entries() -> None:
    parsed = parse_dtc_list(["P0301", "GARBAGE", "U0100", ""])
    assert [d.raw for d in parsed] == ["P0301", "U0100"]


def test_parse_list_tolerates_non_string_entries() -> None:
    """Real payloads contain surprises; the parser must not crash on them."""
    assert [d.raw for d in parse_dtc_list(["P0301", None, 42, {"a": 1}])] == ["P0301"]


@pytest.mark.parametrize("value", [None, "P0301", 42, {"a": 1}])
def test_parse_list_of_a_non_list_is_empty(value: object) -> None:
    assert parse_dtc_list(value) == ()


def test_parse_list_of_empty_is_empty() -> None:
    assert parse_dtc_list([]) == ()


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------

_valid_code = st.builds(
    lambda s, a, b, c: f"{s}{a}{b}{c}",
    st.sampled_from("PBCU"),
    st.sampled_from("0123"),
    st.sampled_from("0123456789ABCDEF"),
    st.sampled_from("0123456789ABCDEF"),
).map(lambda s: s + "0")


@given(_valid_code)
@settings(max_examples=300)
def test_property_well_formed_codes_always_parse(code: str) -> None:
    dtc = parse_dtc(code)
    assert dtc is not None
    assert dtc.raw == code


@given(_valid_code)
@settings(max_examples=300)
def test_property_raw_round_trips(code: str) -> None:
    """Parsing must never alter the code it reports back."""
    dtc = parse_dtc(code.lower())
    assert dtc is not None
    assert dtc.raw == code.upper()


@given(st.lists(_valid_code, max_size=20))
@settings(max_examples=200)
def test_property_list_parsing_preserves_order_and_count(codes: list[str]) -> None:
    parsed = parse_dtc_list(codes)
    assert [d.raw for d in parsed] == codes
