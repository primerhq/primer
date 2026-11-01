"""Shared row iteration for the data migrations (M8).

A migration reads a table through ``Storage.list``. Offset paging re-scans the skipped rows for every page and asks for a ``COUNT(*)`` nobody reads,
so a table of N rows costs O(N^2 / page) row reads and N / page counts. Keyset paging (``CursorPage``, ordered by id) reads each row once, and an
update of a row (its id never changes) cannot make a later page skip or repeat one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from primer.model.storage import CursorPage, Predicate

PAGE = 200


async def iter_rows(storage: Any, *, predicate: Predicate | None = None, page_size: int = PAGE) -> AsyncIterator[Any]:
    """Every row of one table (or only those matching ``predicate``), a page at a time (never the whole table at once).

    A step may write the row it was handed: the next page seeks past its id, so the write cannot make a row skip or repeat."""
    cursor: str | None = None
    while True:
        page = await storage.find(predicate, CursorPage(cursor=cursor, length=page_size))
        for row in page.items:
            yield row
        cursor = page.next_cursor
        if cursor is None:
            return


async def read_all(storage: Any, *, page_size: int = PAGE) -> list[Any]:
    """Every row as a list, for the migrations that must see the whole table before writing (an owner election, a heir rule)."""
    return [row async for row in iter_rows(storage, page_size=page_size)]


__all__ = ["PAGE", "iter_rows", "read_all"]
