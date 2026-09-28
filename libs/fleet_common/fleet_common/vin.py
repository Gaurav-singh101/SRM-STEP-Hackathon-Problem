"""VIN validation, ISO 3779 / ISO 3780 with the North American check digit.

Required by the hackathon PDF S9 ("Validate VINs (17 characters, no I/O/Q,
check digit)"). Pure and deterministic, so it is property-testable (CLAUDE.md S12).

KNOWN LIMITATION, by design: the position-9 check digit is mandated by US FMVSS
and enforced in North America, but it is not universal. Many European VINs carry
no valid check digit and legitimately fail ``is_valid_vin`` while being genuine
VINs. Our fleet is synthetic so we control this, and the simulator generates VINs
with correct check digits. A real multi-region deployment would need the checksum
to be advisory (a DLQ reason code to review) rather than a hard reject.

Complexity: O(1) - exactly 17 positions, fixed-size lookup tables.
"""

from __future__ import annotations

import re
from typing import Final

VIN_LENGTH: Final[int] = 17

#: I, O and Q are excluded from VINs because they are confusable with 1 and 0.
VIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

#: Position 9 (index 8) carries the check digit; 'X' encodes a remainder of 10.
CHECK_DIGIT_INDEX: Final[int] = 8

#: Transliteration table (ISO 3779). Letters map to digits for the checksum.
_TRANSLITERATION: Final[dict[str, int]] = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "J": 1,
    "K": 2,
    "L": 3,
    "M": 4,
    "N": 5,
    "P": 7,
    "R": 9,
    "S": 2,
    "T": 3,
    "U": 4,
    "V": 5,
    "W": 6,
    "X": 7,
    "Y": 8,
    "Z": 9,
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
}

#: Positional weights; the check-digit position itself is weighted 0.
_WEIGHTS: Final[tuple[int, ...]] = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def has_valid_vin_format(vin: str) -> bool:
    """Length and alphabet only, no checksum. Cheap pre-filter for the hot path."""
    return bool(VIN_PATTERN.fullmatch(vin))


def compute_check_digit(vin: str) -> str:
    """Return the expected check digit for ``vin``: '0'-'9' or 'X'.

    Raises:
        ValueError: if ``vin`` is not 17 characters of the VIN alphabet.
    """
    if not has_valid_vin_format(vin):
        raise ValueError(f"not a well-formed VIN: {vin!r}")
    total = sum(_TRANSLITERATION[ch] * w for ch, w in zip(vin, _WEIGHTS, strict=True))
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def is_valid_vin(vin: str) -> bool:
    """True when ``vin`` is well-formed AND its check digit agrees.

    Used by the event model so a corrupt VIN is routed to the DLQ with a reason
    code instead of contaminating per-vehicle aggregates (CLAUDE.md S8).
    """
    if not has_valid_vin_format(vin):
        return False
    return vin[CHECK_DIGIT_INDEX] == compute_check_digit(vin)


def repair_check_digit(vin: str) -> str:
    """Return ``vin`` with position 9 replaced by its correct check digit.

    For the synthetic simulator only: it lets us generate structurally valid
    VINs from a seeded random base. Never call this on ingest - a real event
    with a bad check digit is bad data, not data to be silently fixed.
    """
    if not has_valid_vin_format(vin):
        raise ValueError(f"not a well-formed VIN: {vin!r}")
    digit = compute_check_digit(vin)
    return vin[:CHECK_DIGIT_INDEX] + digit + vin[CHECK_DIGIT_INDEX + 1 :]
