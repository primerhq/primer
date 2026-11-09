"""Bounded helpers for tests that must compare every row in a storage."""

from __future__ import annotations

from typing import Any

from primer.model.storage import OffsetPage


async def all_storage_rows(storage: Any, *, page_size: int = 200) -> list[Any]:
    """Read every row without silently truncating at one page."""

    rows: list[Any] = []
    offset = 0
    while True:
        page = await storage.list(
            OffsetPage(offset=offset, length=page_size, want_total=False)
        )
        rows.extend(page.items)
        if len(page.items) < page_size:
            return rows
        offset += len(page.items)


__all__ = ["all_storage_rows"]
