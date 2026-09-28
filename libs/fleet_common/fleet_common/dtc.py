"""OBD-II Diagnostic Trouble Code parsing (hackathon PDF S9).

A DTC such as ``P0301`` decomposes into system, code type, subsystem and a
fault number. Parsing it lets the UI group faults by subsystem instead of
showing opaque strings.

Complexity: O(1) per code - fixed 5-character grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DTC_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<system>[PBCU])(?P<digits>[0-3][0-9A-F]{3})$"
)


class DtcSystem(StrEnum):
    POWERTRAIN = "P"
    BODY = "B"
    CHASSIS = "C"
    NETWORK = "U"


#: Second character: 0 and 2 are generic (SAE-defined), 1 and 3 manufacturer-specific.
_GENERIC_TYPES: Final[frozenset[str]] = frozenset({"0", "2"})

#: Third character for powertrain codes -> subsystem, per SAE J2012.
_POWERTRAIN_SUBSYSTEM: Final[dict[str, str]] = {
    "0": "fuel_and_air_metering_or_auxiliary",
    "1": "fuel_and_air_metering",
    "2": "fuel_and_air_metering_injector",
    "3": "ignition_or_misfire",
    "4": "auxiliary_emission_controls",
    "5": "speed_idle_control_and_inputs",
    "6": "computer_and_output_circuit",
    "7": "transmission",
    "8": "transmission",
    "9": "transmission",
    "A": "hybrid_propulsion",
    "B": "hybrid_propulsion",
    "C": "hybrid_propulsion",
}


@dataclass(frozen=True, slots=True)
class Dtc:
    """A parsed diagnostic trouble code."""

    raw: str
    system: DtcSystem
    is_generic: bool
    subsystem: str | None
    fault_number: str

    @property
    def is_idle_related(self) -> bool:
        """True for the 'speed/idle control' family.

        These are the codes a fleet finance user can act on alongside an idle
        cost figure, so the UI surfaces them next to idle events.
        """
        return self.system is DtcSystem.POWERTRAIN and self.subsystem == (
            "speed_idle_control_and_inputs"
        )


def parse_dtc(code: str) -> Dtc | None:
    """Parse one DTC, or return ``None`` if ``code`` is not a valid DTC.

    Returns ``None`` rather than raising: a malformed code inside an otherwise
    good event should not fail the whole event (CLAUDE.md S8).
    """
    match = DTC_PATTERN.fullmatch(code.strip().upper())
    if match is None:
        return None
    system = DtcSystem(match.group("system"))
    digits = match.group("digits")
    subsystem = _POWERTRAIN_SUBSYSTEM.get(digits[1]) if system is DtcSystem.POWERTRAIN else None
    return Dtc(
        raw=match.group(0),
        system=system,
        is_generic=digits[0] in _GENERIC_TYPES,
        subsystem=subsystem,
        fault_number=digits[2:],
    )


def parse_dtc_list(codes: object) -> tuple[Dtc, ...]:
    """Parse a list of codes, silently dropping unparseable entries."""
    if not isinstance(codes, (list, tuple)):
        return ()
    parsed = (parse_dtc(c) for c in codes if isinstance(c, str))
    return tuple(d for d in parsed if d is not None)
