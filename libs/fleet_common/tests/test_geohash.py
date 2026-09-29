"""Tests for geohash encoding (PDF S9).

The property everything rests on: **a shared prefix means physical proximity**. That is
what lets depot grouping be a `GROUP BY` on a string rather than a spatial index, and
what makes truncation a principled way to coarsen a location.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.domain.geohash import (
    BASE32,
    DEPOT_PRECISION,
    MAX_PRECISION,
    approximate_cell_size_metres,
    coarsen,
    geohash,
    share_cell,
)

#: A published reference point: the geohash of (57.64911, 10.40744) is "u4pruydqqvj".
#: An external oracle, so the implementation is not merely self-consistent.
REFERENCE_LAT = 57.64911
REFERENCE_LON = 10.40744
REFERENCE_HASH = "u4pruydqqvj"


def test_matches_a_published_reference_value() -> None:
    """Without an external oracle a bit-interleaving bug would pass every other test."""
    assert geohash(REFERENCE_LAT, REFERENCE_LON, precision=11) == REFERENCE_HASH


@pytest.mark.parametrize("precision", range(1, 12))
def test_prefixes_of_the_reference_match_at_every_precision(precision: int) -> None:
    """A lower precision is exactly a prefix - which is what makes truncation exact
    rather than approximate."""
    assert geohash(REFERENCE_LAT, REFERENCE_LON, precision) == REFERENCE_HASH[:precision]


# --------------------------------------------------------------------------
# The proximity property
# --------------------------------------------------------------------------


def test_nearby_points_share_a_depot_cell() -> None:
    """Two vehicles parked on the same forecourt must group together, or the depot view
    would scatter one site across several rows."""
    a = geohash(12.9716, 77.5946)
    b = geohash(12.9740, 77.5950)
    assert share_cell(a, b, DEPOT_PRECISION)


def test_distant_points_do_not_share_a_depot_cell() -> None:
    a = geohash(12.9716, 77.5946)
    far = geohash(13.1500, 77.7000)
    assert not share_cell(a, far, DEPOT_PRECISION)


def test_coarsening_merges_cells_that_were_distinct() -> None:
    """Each character dropped widens the cell roughly eightfold."""
    a = geohash(12.9716, 77.5946, precision=8)
    b = geohash(12.9900, 77.6100, precision=8)
    assert a != b
    assert coarsen(a, 4) == coarsen(b, 4)


def test_antimeridian_points_do_not_accidentally_share_a_cell() -> None:
    """Longitudes 179 and -179 are close on the ground but at opposite ends of the
    encoding range, so they must not be merged."""
    east = geohash(0.0, 179.9)
    west = geohash(0.0, -179.9)
    assert east != west


# --------------------------------------------------------------------------
# Missing and invalid input
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("lat", "lon"), [(None, 1.0), (1.0, None), (None, None)])
def test_missing_coordinates_return_none(lat: float | None, lon: float | None) -> None:
    """A vehicle in a tunnel has no fix. That is missing data, not an error."""
    assert geohash(lat, lon) is None


def test_coarsening_none_returns_none() -> None:
    assert coarsen(None, 4) is None


def test_share_cell_is_false_when_either_side_is_missing() -> None:
    """Two unknown locations are not evidence of proximity."""
    assert not share_cell(None, "tdr1v9", DEPOT_PRECISION)
    assert not share_cell("tdr1v9", None, DEPOT_PRECISION)
    assert not share_cell(None, None, DEPOT_PRECISION)


@pytest.mark.parametrize("lat", [91.0, -91.0, 1000.0])
def test_out_of_range_latitude_is_refused(lat: float) -> None:
    with pytest.raises(ValueError, match="latitude out of range"):
        geohash(lat, 0.0)


@pytest.mark.parametrize("lon", [181.0, -181.0])
def test_out_of_range_longitude_is_refused(lon: float) -> None:
    with pytest.raises(ValueError, match="longitude out of range"):
        geohash(0.0, lon)


@pytest.mark.parametrize("precision", [0, -1, MAX_PRECISION + 1])
def test_invalid_precision_is_refused(precision: int) -> None:
    with pytest.raises(ValueError, match="precision must be"):
        geohash(0.0, 0.0, precision)


def test_coarsening_to_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        coarsen("tdr1v9", 0)


# --------------------------------------------------------------------------
# Cell size guidance
# --------------------------------------------------------------------------


def test_depot_precision_gives_roughly_kilometre_cells() -> None:
    """Large enough that a whole depot forecourt is one group, small enough not to
    merge two depots in the same town."""
    size = approximate_cell_size_metres(DEPOT_PRECISION)
    assert 500 <= size <= 2_000


def test_cell_size_shrinks_as_precision_grows() -> None:
    sizes = [approximate_cell_size_metres(p) for p in range(1, 10)]
    assert sizes == sorted(sizes, reverse=True)


def test_unknown_precision_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown precision"):
        approximate_cell_size_metres(0)


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------

_lat = st.floats(min_value=-90.0, max_value=90.0, allow_nan=False, allow_infinity=False)
_lon = st.floats(min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False)


@given(_lat, _lon, st.integers(min_value=1, max_value=MAX_PRECISION))
@settings(max_examples=400)
def test_property_output_length_equals_precision(lat: float, lon: float, precision: int) -> None:
    result = geohash(lat, lon, precision)
    assert result is not None
    assert len(result) == precision


@given(_lat, _lon, st.integers(min_value=1, max_value=MAX_PRECISION))
@settings(max_examples=400)
def test_property_output_uses_only_the_base32_alphabet(
    lat: float, lon: float, precision: int
) -> None:
    """The alphabet excludes a, i, l and o because they are confusable when read aloud
    or transcribed, so a stray one would mean the encoding is wrong."""
    result = geohash(lat, lon, precision)
    assert result is not None
    assert set(result) <= set(BASE32)


@given(_lat, _lon, st.integers(min_value=1, max_value=8))
@settings(max_examples=400)
def test_property_lower_precision_is_always_a_prefix(
    lat: float, lon: float, precision: int
) -> None:
    """The invariant depot grouping and privacy coarsening both depend on."""
    full = geohash(lat, lon, MAX_PRECISION)
    short = geohash(lat, lon, precision)
    assert full is not None and short is not None
    assert full.startswith(short)
    assert coarsen(full, precision) == short


@given(_lat, _lon)
@settings(max_examples=300)
def test_property_a_point_always_shares_a_cell_with_itself(lat: float, lon: float) -> None:
    value = geohash(lat, lon)
    assert share_cell(value, value, DEPOT_PRECISION)


@given(_lat, _lon)
@settings(max_examples=300)
def test_property_encoding_is_deterministic(lat: float, lon: float) -> None:
    assert geohash(lat, lon) == geohash(lat, lon)
