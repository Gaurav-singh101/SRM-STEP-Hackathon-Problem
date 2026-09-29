"""Keyset pagination with opaque cursors.

CLAUDE.md S3 forbids offset pagination on large tables, and S10 requires opaque
cursors with a hard maximum page size. Both rules earn their place:

**Why not OFFSET.** ``OFFSET 100000`` makes PostgreSQL walk and discard 100,000 rows
before returning anything, so page 2,000 costs 2,000 times page 1. On a table
partitioned by time with billions of rows, deep pages simply stop responding. Keyset
pagination instead says "everything after this exact row", which an index satisfies in
constant time at any depth.

**Why OFFSET is also wrong, not merely slow.** Rows arriving while a user pages
through shift the window, so an offset-paged list silently skips and repeats rows.
A keyset cursor is anchored to a row, so concurrent inserts cannot corrupt the sequence.

**Why opaque.** The cursor encodes internal sort keys. Leaving it readable invites
clients to construct their own, which freezes the sort order into the public API
forever. Base64url of a compact JSON body is enough: this is an encoding, not a
security boundary, and the docstrings say so rather than implying otherwise.

Pure: no I/O, no clock. The SQL that consumes these lives in the adapters.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import orjson

#: Hard ceiling on page size, whatever a caller asks for. An unbounded page is both a
#: denial-of-service vector and a way to defeat pagination entirely (CLAUDE.md S10).
MAX_PAGE_SIZE: Final[int] = 200
DEFAULT_PAGE_SIZE: Final[int] = 50

#: Bumped if the cursor body ever changes shape, so an old cursor is rejected cleanly
#: rather than misread as the new format.
CURSOR_VERSION: Final[int] = 1


class InvalidCursorError(ValueError):
    """Raised when a cursor is malformed, truncated or of an unknown version.

    Distinct from a generic ValueError so the API can answer 400 rather than 500: a
    bad cursor is the client's mistake, not the server's.
    """


@dataclass(frozen=True, slots=True)
class Cursor:
    """Position in a result set, anchored to one row.

    Two components because a single sort key is not unique: many idle events share a
    timestamp, and paging on time alone would skip or repeat the ties. The primary key
    breaks them, which makes the ordering total and the cursor exact.
    """

    #: Value of the primary sort column on the last row of the previous page.
    sort_value: str
    #: Tie-breaker: the row's primary key.
    row_id: int

    def encode(self) -> str:
        """Serialise to an opaque, URL-safe token."""
        body = orjson.dumps({"v": CURSOR_VERSION, "s": self.sort_value, "i": self.row_id})
        return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        """Parse an opaque token.

        Raises:
            InvalidCursorError: for anything that is not a cursor this version emitted.
        """
        if not token:
            raise InvalidCursorError("cursor is empty")
        try:
            # Restore the padding stripped by encode().
            padded = token + "=" * (-len(token) % 4)
            body: Any = orjson.loads(base64.urlsafe_b64decode(padded))
        except (binascii.Error, ValueError, orjson.JSONDecodeError) as exc:
            raise InvalidCursorError(f"cursor is not decodable: {exc}") from exc

        if not isinstance(body, dict):
            raise InvalidCursorError("cursor body is not an object")
        if body.get("v") != CURSOR_VERSION:
            raise InvalidCursorError(
                f"cursor version {body.get('v')!r} is not supported; restart pagination"
            )
        sort_value = body.get("s")
        row_id = body.get("i")
        if not isinstance(sort_value, str) or not isinstance(row_id, int):
            raise InvalidCursorError("cursor is missing its sort key or row id")
        return cls(sort_value=sort_value, row_id=row_id)

    @classmethod
    def from_row(cls, sort_value: datetime | str | int, row_id: int) -> Cursor:
        """Build a cursor from the last row of a page."""
        # ISO-8601 so the value round-trips exactly. A float epoch would lose
        # microseconds and the cursor would drift onto a neighbouring row.
        encoded = sort_value.isoformat() if isinstance(sort_value, datetime) else str(sort_value)
        return cls(sort_value=encoded, row_id=row_id)

    @property
    def sort_datetime(self) -> datetime:
        """Interpret the sort value as a timestamp.

        Raises:
            InvalidCursorError: if it is not a parseable timestamp.
        """
        try:
            return datetime.fromisoformat(self.sort_value)
        except ValueError as exc:
            raise InvalidCursorError(
                f"cursor sort value {self.sort_value!r} is not a timestamp"
            ) from exc


def clamp_page_size(requested: int | None, *, default: int = DEFAULT_PAGE_SIZE) -> int:
    """Return a safe page size.

    Clamps rather than rejecting: a caller asking for 10,000 wants "as many as
    possible", and answering 200 serves them better than a 400 would.
    """
    if requested is None:
        return default
    if requested < 1:
        return default
    return min(requested, MAX_PAGE_SIZE)


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results plus the cursor for the next.

    ``next_cursor`` is ``None`` exactly when there are no further rows, so a client
    loops until it is absent and needs no separate total count. That matters: an exact
    ``COUNT(*)`` over a partitioned table of billions of rows is itself an expensive
    scan, and it would be stale the moment it was computed.
    """

    items: list[T]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    @property
    def size(self) -> int:
        return len(self.items)


def build_page[T](
    rows: list[T],
    *,
    page_size: int,
    cursor_of: Callable[[T], Cursor],
) -> Page[T]:
    """Trim an over-fetched result set into a page and derive the next cursor.

    The caller fetches ``page_size + 1`` rows. The extra row is the existence proof
    that another page follows, and is then discarded - which avoids a second query or
    a count, and cannot disagree with itself.

    ``cursor_of`` maps the last returned row to a ``Cursor``.
    """
    if page_size < 1:
        raise ValueError("page_size must be at least 1")

    has_more = len(rows) > page_size
    items = rows[:page_size]
    if not has_more or not items:
        return Page(items=items, next_cursor=None)

    cursor = cursor_of(items[-1])
    return Page(items=items, next_cursor=cursor.encode())
