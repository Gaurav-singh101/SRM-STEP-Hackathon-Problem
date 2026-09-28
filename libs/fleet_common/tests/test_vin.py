"""Tests for VIN validation (hackathon PDF S9).

The check-digit implementation is verified against published VINs as independent
oracles, not only against itself - a self-consistent but wrong checksum would
otherwise pass every property test.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.vin import (
    VIN_LENGTH,
    compute_check_digit,
    has_valid_vin_format,
    is_valid_vin,
    repair_check_digit,
)

#: The VIN used in the hackathon PDF S8 illustrative event. An external oracle:
#: if our transliteration or weights were wrong, this would not validate.
PDF_VIN = "1HGCM82633A004352"

#: All-ones is a known fixed point of the ISO 3779 checksum (weights sum to 89,
#: 89 mod 11 == 1), so it validates. A second oracle independent of the first.
ALL_ONES_VIN = "1" * 17

VIN_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


@pytest.mark.parametrize("vin", [PDF_VIN, ALL_ONES_VIN, "5GZCZ43D13S812715"])
def test_published_vins_validate(vin: str) -> None:
    assert is_valid_vin(vin), f"{vin} should validate against the ISO 3779 check digit"


def test_pdf_vin_check_digit_matches_its_own_position_9() -> None:
    assert compute_check_digit(PDF_VIN) == PDF_VIN[8] == "3"


def test_wrong_length_is_rejected() -> None:
    assert not has_valid_vin_format("1HGCM82633A00435")
    assert not has_valid_vin_format(PDF_VIN + "X")


@pytest.mark.parametrize("letter", ["I", "O", "Q"])
def test_confusable_letters_are_rejected(letter: str) -> None:
    """I, O and Q are excluded because they are confusable with 1 and 0."""
    vin = letter + PDF_VIN[1:]
    assert not has_valid_vin_format(vin)
    assert not is_valid_vin(vin)


def test_lowercase_is_rejected() -> None:
    """Normalisation is the caller's job; the validator does not silently upcase."""
    assert not has_valid_vin_format(PDF_VIN.lower())


def test_single_character_corruption_is_detected() -> None:
    """The point of a check digit: a transcription slip must not pass."""
    corrupted = "1HGCM82634A004352"  # index 9 (a body character) changed 3 -> 4
    assert has_valid_vin_format(corrupted)
    assert not is_valid_vin(corrupted)


def test_compute_check_digit_rejects_malformed_input() -> None:
    with pytest.raises(ValueError, match="not a well-formed VIN"):
        compute_check_digit("TOO-SHORT")


def test_check_digit_position_is_weighted_zero() -> None:
    """Position 9 does not contribute to its own checksum.

    So the digit found there cannot influence the digit computed for it - which
    is exactly why a corrupted check digit is detectable.
    """
    digits = {compute_check_digit("0" * 8 + d + "0" * 8) for d in "0123456789"}
    assert digits == {"0"}


def test_remainder_of_ten_encodes_as_x() -> None:
    """Remainder 10 is written 'X', so the check digit is not always numeric."""
    remainder_ten_vin = "1HGCM8260A0043507"
    assert compute_check_digit(remainder_ten_vin) == "X"
    # Placed at position 9 the VIN validates, X and all.
    assert is_valid_vin("1HGCM826XA0043507")


#: PDF_VIN with position 9 (index 8) corrupted from '3' to '0'.
BROKEN_CHECK_DIGIT_VIN = "1HGCM82603A004352"


def test_repair_check_digit_makes_a_vin_valid() -> None:
    assert not is_valid_vin(BROKEN_CHECK_DIGIT_VIN)
    assert is_valid_vin(repair_check_digit(BROKEN_CHECK_DIGIT_VIN))


def test_repair_preserves_every_other_position() -> None:
    """Only index 8 may change; touching the body would rewrite the vehicle id."""
    repaired = repair_check_digit(BROKEN_CHECK_DIGIT_VIN)
    assert repaired == PDF_VIN
    assert repaired[:8] == BROKEN_CHECK_DIGIT_VIN[:8]
    assert repaired[9:] == BROKEN_CHECK_DIGIT_VIN[9:]


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------

_vin_body = st.text(alphabet=VIN_ALPHABET, min_size=VIN_LENGTH, max_size=VIN_LENGTH)


@given(_vin_body)
@settings(max_examples=400)
def test_property_repair_always_yields_a_valid_vin(candidate: str) -> None:
    assert is_valid_vin(repair_check_digit(candidate))


@given(_vin_body)
@settings(max_examples=400)
def test_property_repair_is_idempotent(candidate: str) -> None:
    once = repair_check_digit(candidate)
    assert repair_check_digit(once) == once


@given(_vin_body)
@settings(max_examples=400)
def test_property_check_digit_is_a_single_valid_character(candidate: str) -> None:
    digit = compute_check_digit(candidate)
    assert len(digit) == 1
    assert digit in "0123456789X"


@given(_vin_body, st.integers(min_value=0, max_value=16))
@settings(max_examples=400)
def test_property_validity_depends_only_on_the_check_digit_position(
    candidate: str, position: int
) -> None:
    """Changing position 9 of a valid VIN to anything else must invalidate it."""
    valid = repair_check_digit(candidate)
    expected = valid[8]
    for replacement in "0123456789X":
        if replacement == expected:
            continue
        mutated = valid[:8] + replacement + valid[9:]
        if not has_valid_vin_format(mutated):
            continue  # 'X' is not in the VIN body alphabet at this position
        assert not is_valid_vin(mutated)
