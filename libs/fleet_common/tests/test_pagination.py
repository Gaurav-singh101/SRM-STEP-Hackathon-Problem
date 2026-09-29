"""Tests for keyset pagination.

The property that matters: **paging through a list must return every row exactly
once**, even while rows are being inserted. That is the guarantee OFFSET cannot make,
and it is the reason CLAUDE.md S3 forbids OFFSET on large tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fleet_common.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    InvalidCursorError,
    build_page,
    clamp_page_size,
)

T0 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Row:
    row_id: int
    at: datetime


def rows(count: int, *, start: int = 1) -> list[Row]:
    """Rows in descending time order, as a real query returns them."""
    return [Row(row_id=i, at=T0 - timedelta(minutes=i)) for i in range(start, start + count)]


def cursor_of(row: Row) -> Cursor:
    return Cursor.from_row(row.at, row.row_id)


# --------------------------------------------------------------------------
# Cursor encoding
# --------------------------------------------------------------------------


def test_cursor_round_trips() -> None:
    original = Cursor(sort_value=T0.isoformat(), row_id=42)
    assert Cursor.decode(original.encode()) == original


def test_cursor_is_url_safe() -> None:
    """It travels in a query string, so it must need no escaping."""
    token = Cursor.from_row(T0, 999_999).encode()
    assert "+" not in token
    assert "/" not in token
    assert "=" not in token


def test_cursor_is_opaque() -> None:
    """Not a security boundary, but it must not invite clients to build their own.

    A readable cursor freezes the sort order into the public API forever.
    """
    token = Cursor.from_row(T0, 42).encode()
    assert "2026" not in token
    assert "42" not in token


def test_timestamp_survives_to_microseconds() -> None:
    """A float epoch would lose precision and drift the cursor onto a neighbouring row."""
    precise = datetime(2026, 9, 29, 12, 0, 0, 123_456, tzinfo=UTC)
    restored = Cursor.decode(Cursor.from_row(precise, 1).encode()).sort_datetime
    assert restored == precise


def test_integer_sort_value_round_trips() -> None:
    assert Cursor.decode(Cursor.from_row(12345, 1).encode()).sort_value == "12345"


@pytest.mark.parametrize(
    "token",
    ["", "!!!not base64!!!", "YWJj", "e30", "bm90LWpzb24"],
)
def test_malformed_cursors_are_rejected(token: str) -> None:
    """A bad cursor is the client's mistake, so it must raise a distinguishable error
    the API can turn into a 400 rather than a 500."""
    with pytest.raises(InvalidCursorError):
        Cursor.decode(token)


def test_cursor_from_a_future_version_is_rejected() -> None:
    """Refused rather than misread as the current format."""
    import base64

    import orjson

    body = orjson.dumps({"v": 99, "s": "x", "i": 1})
    token = base64.urlsafe_b64encode(body).decode().rstrip("=")
    with pytest.raises(InvalidCursorError, match="not supported"):
        Cursor.decode(token)


def test_cursor_missing_its_row_id_is_rejected() -> None:
    import base64

    import orjson

    body = orjson.dumps({"v": 1, "s": "x"})
    token = base64.urlsafe_b64encode(body).decode().rstrip("=")
    with pytest.raises(InvalidCursorError, match="missing"):
        Cursor.decode(token)


def test_non_timestamp_sort_value_raises_when_read_as_one() -> None:
    cursor = Cursor(sort_value="not-a-date", row_id=1)
    with pytest.raises(InvalidCursorError, match="not a timestamp"):
        _ = cursor.sort_datetime


# --------------------------------------------------------------------------
# Page size
# --------------------------------------------------------------------------


def test_page_size_defaults_when_absent() -> None:
    assert clamp_page_size(None) == DEFAULT_PAGE_SIZE


def test_page_size_is_capped() -> None:
    """An unbounded page is a denial-of-service vector and defeats pagination."""
    assert clamp_page_size(10_000) == MAX_PAGE_SIZE


@pytest.mark.parametrize("requested", [0, -1, -100])
def test_nonsense_page_size_falls_back_to_the_default(requested: int) -> None:
    """Clamped rather than rejected: the caller wants rows, not an error."""
    assert clamp_page_size(requested) == DEFAULT_PAGE_SIZE


def test_reasonable_page_size_is_respected() -> None:
    assert clamp_page_size(25) == 25


# --------------------------------------------------------------------------
# Page building
# --------------------------------------------------------------------------


def test_over_fetched_row_signals_another_page_and_is_discarded() -> None:
    """The extra row proves a further page exists without a second query or a COUNT."""
    page = build_page(rows(4), page_size=3, cursor_of=cursor_of)
    assert page.size == 3
    assert page.has_more
    assert page.next_cursor is not None


def test_exact_fit_reports_no_further_page() -> None:
    page = build_page(rows(3), page_size=3, cursor_of=cursor_of)
    assert page.size == 3
    assert not page.has_more
    assert page.next_cursor is None


def test_partial_page_reports_no_further_page() -> None:
    page = build_page(rows(2), page_size=5, cursor_of=cursor_of)
    assert page.size == 2
    assert not page.has_more


def test_empty_result_is_a_valid_empty_page() -> None:
    page: object = build_page([], page_size=10, cursor_of=cursor_of)
    assert page.items == []  # type: ignore[attr-defined]
    assert page.next_cursor is None  # type: ignore[attr-defined]


def test_cursor_points_at_the_last_returned_row() -> None:
    """Not at the over-fetched row: the next page must resume from what the client
    actually received, or one row is skipped on every page boundary."""
    data = rows(4)
    page = build_page(data, page_size=3, cursor_of=cursor_of)
    assert page.next_cursor is not None
    decoded = Cursor.decode(page.next_cursor)
    assert decoded.row_id == data[2].row_id


def test_zero_page_size_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        build_page(rows(1), page_size=0, cursor_of=cursor_of)


# --------------------------------------------------------------------------
# The guarantee OFFSET cannot make
# --------------------------------------------------------------------------


def paginate_all(data: list[Row], page_size: int) -> list[Row]:
    """Walk every page, simulating what the SQL keyset predicate does."""
    collected: list[Row] = []
    cursor: Cursor | None = None

    while True:
        if cursor is None:
            window = data
        else:
            anchor = (cursor.sort_datetime, cursor.row_id)
            window = [r for r in data if (r.at, r.row_id) < anchor]

        page = build_page(window[: page_size + 1], page_size=page_size, cursor_of=cursor_of)
        collected.extend(page.items)
        if not page.has_more:
            return collected
        assert page.next_cursor is not None
        cursor = Cursor.decode(page.next_cursor)


def test_paging_returns_every_row_exactly_once() -> None:
    data = rows(97)
    collected = paginate_all(data, page_size=10)
    assert [r.row_id for r in collected] == [r.row_id for r in data]


def test_rows_inserted_mid_pagination_do_not_shift_the_window() -> None:
    """The decisive difference from OFFSET.

    With OFFSET, a row inserted at the head pushes everything down by one and the next
    page silently repeats a row already shown. A keyset cursor is anchored to a row, so
    newer rows simply do not appear - they sort ahead of the anchor.
    """
    data = rows(20)
    page_size = 5

    first = build_page(data[: page_size + 1], page_size=page_size, cursor_of=cursor_of)
    assert first.next_cursor is not None
    cursor = Cursor.decode(first.next_cursor)

    # A newer row arrives at the head of the descending order.
    newcomer = Row(row_id=999, at=T0 + timedelta(minutes=5))
    grown = sorted([*data, newcomer], key=lambda r: (r.at, r.row_id), reverse=True)

    anchor = (cursor.sort_datetime, cursor.row_id)
    window = [r for r in grown if (r.at, r.row_id) < anchor]
    second = build_page(window[: page_size + 1], page_size=page_size, cursor_of=cursor_of)

    first_ids = {r.row_id for r in first.items}
    second_ids = {r.row_id for r in second.items}
    assert first_ids.isdisjoint(second_ids), "a row was repeated across pages"
    assert 999 not in second_ids, "a row inserted after the anchor leaked into page 2"


def test_ties_on_the_sort_column_are_still_ordered_totally() -> None:
    """Many idle events share a timestamp. Without the primary key in the ordering the
    comparison is not a total order and paging over ties skips or repeats rows."""
    same_time = [Row(row_id=i, at=T0) for i in range(1, 11)]
    same_time.sort(key=lambda r: (r.at, r.row_id), reverse=True)
    collected = paginate_all(same_time, page_size=3)
    assert [r.row_id for r in collected] == [r.row_id for r in same_time]
    assert len({r.row_id for r in collected}) == 10


# --------------------------------------------------------------------------
# Property-based invariants
# --------------------------------------------------------------------------


@given(
    st.integers(min_value=0, max_value=200),
    st.integers(min_value=1, max_value=25),
)
@settings(max_examples=200, deadline=None)
def test_property_pagination_is_lossless_for_any_shape(count: int, page_size: int) -> None:
    data = rows(count)
    collected = paginate_all(data, page_size=page_size)
    assert [r.row_id for r in collected] == [r.row_id for r in data]


@given(st.integers(min_value=0, max_value=200), st.integers(min_value=1, max_value=25))
@settings(max_examples=200, deadline=None)
def test_property_no_row_is_ever_returned_twice(count: int, page_size: int) -> None:
    ids = [r.row_id for r in paginate_all(rows(count), page_size=page_size)]
    assert len(ids) == len(set(ids))


@given(st.integers(min_value=-1000, max_value=100_000))
@settings(max_examples=300)
def test_property_page_size_is_always_within_bounds(requested: int) -> None:
    assert 1 <= clamp_page_size(requested) <= MAX_PAGE_SIZE


@given(st.integers(min_value=0, max_value=2**40), st.integers(min_value=0, max_value=2**31))
@settings(max_examples=300)
def test_property_any_cursor_round_trips(row_id: int, offset: int) -> None:
    at = T0 + timedelta(seconds=offset % 10_000_000)
    original = Cursor.from_row(at, row_id)
    assert Cursor.decode(original.encode()) == original
