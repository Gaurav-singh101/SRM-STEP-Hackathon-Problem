"""Geohash encoding for location grouping.

Named by the hackathon PDF S9 under streaming algorithms. It earns its place here for a
specific job: grouping idle events by *where* they happened, so a fleet manager can see
that most of the idle cost is at one depot rather than spread across the fleet. That is
a different answer from "which vehicles idle most", and often a more actionable one -
one depot with a bad loading process is fixable, two hundred drivers are not.

A geohash interleaves latitude and longitude bits into one string, so **a shared prefix
means physical proximity**. Grouping by prefix is then an ordinary string operation: no
spatial index, no distance calculation, and it works directly in a SQL `GROUP BY`.

It also serves privacy. Truncating a geohash is a principled way to coarsen a location:
each character dropped widens the cell roughly eightfold, so precision is reduced in
defined steps rather than by rounding decimal degrees, whose meaning varies with
latitude (CLAUDE.md S11).

Complexity: O(precision) per encode, with precision bounded at 12.
"""

from __future__ import annotations

from typing import Final

#: Base-32 alphabet used by geohash: digits plus lowercase letters, excluding a, i, l
#: and o because they are confusable when read aloud or transcribed.
BASE32: Final[str] = "0123456789bcdefghjkmnpqrstuvwxyz"

#: Approximate cell size per precision level. Quoted as guidance for choosing a level;
#: cells are not square and narrow towards the poles, so these are latitude-dependent.
CELL_SIZE_METRES: Final[dict[int, int]] = {
    1: 5_000_000,
    2: 1_250_000,
    3: 156_000,
    4: 39_000,
    5: 4_900,
    6: 1_200,
    7: 152,
    8: 38,
    9: 5,
}

#: Default for depot grouping. ~1.2 km cells: large enough that a vehicle parked
#: anywhere on a depot forecourt lands in the same group, small enough not to merge two
#: depots in the same town.
DEPOT_PRECISION: Final[int] = 6

MAX_PRECISION: Final[int] = 12


def geohash(lat: float | None, lon: float | None, precision: int = DEPOT_PRECISION) -> str | None:
    """Encode a coordinate pair as a geohash string.

    Returns ``None`` when either coordinate is missing, which happens routinely: a
    vehicle in a tunnel or an underground car park has no fix, and that is missing data
    rather than an error.

    Raises:
        ValueError: for a precision outside 1-12, or an out-of-range coordinate.
    """
    if lat is None or lon is None:
        return None
    if not 1 <= precision <= MAX_PRECISION:
        raise ValueError(f"precision must be between 1 and {MAX_PRECISION}, got {precision}")
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude out of range: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"longitude out of range: {lon}")

    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    encoded: list[str] = []
    bits = 0
    bit_count = 0
    # Longitude first, then alternating: the standard interleaving, and the reason a
    # shared prefix implies proximity in both axes at once.
    use_longitude = True

    while len(encoded) < precision:
        if use_longitude:
            midpoint = (lon_range[0] + lon_range[1]) / 2
            if lon > midpoint:
                bits = (bits << 1) | 1
                lon_range[0] = midpoint
            else:
                bits <<= 1
                lon_range[1] = midpoint
        else:
            midpoint = (lat_range[0] + lat_range[1]) / 2
            if lat > midpoint:
                bits = (bits << 1) | 1
                lat_range[0] = midpoint
            else:
                bits <<= 1
                lat_range[1] = midpoint

        use_longitude = not use_longitude
        bit_count += 1

        if bit_count == 5:
            encoded.append(BASE32[bits])
            bits = 0
            bit_count = 0

    return "".join(encoded)


def coarsen(value: str | None, precision: int) -> str | None:
    """Truncate a geohash to a lower precision.

    Truncation is exact: a prefix of a geohash is the geohash of the containing cell, so
    this is a genuine widening rather than an approximation.
    """
    if value is None:
        return None
    if precision < 1:
        raise ValueError("precision must be at least 1")
    return value[:precision]


def share_cell(first: str | None, second: str | None, precision: int) -> bool:
    """Whether two geohashes fall in the same cell at ``precision``."""
    if first is None or second is None:
        return False
    return coarsen(first, precision) == coarsen(second, precision)


def approximate_cell_size_metres(precision: int) -> int:
    """Rough cell width at a precision level, for choosing one.

    Deliberately named *approximate*: cells narrow towards the poles, so a single number
    cannot be exact for every latitude.
    """
    if precision in CELL_SIZE_METRES:
        return CELL_SIZE_METRES[precision]
    if precision > max(CELL_SIZE_METRES):
        return 1
    raise ValueError(f"unknown precision {precision}")
