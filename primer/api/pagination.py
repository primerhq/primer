"""Pagination + predicate translators between HTTP and the storage layer."""

from __future__ import annotations

from fastapi import Query
from pydantic import BaseModel, Field

from primer.model.except_ import BadRequestError
from primer.model.storage import (
    CursorPage,
    OffsetPage,
    OrderBy,
    PageRequest,
    Predicate,
)


def parse_page(
    limit: int = Query(default=20, ge=1, le=200, description="Page size."),
    offset: int | None = Query(
        default=None,
        ge=0,
        description="Offset pagination cursor. Mutually exclusive with `cursor`.",
    ),
    cursor: str | None = Query(
        default=None,
        description="Cursor pagination token from the previous page.",
    ),
) -> PageRequest:
    """Translate query params into the storage-layer :class:`PageRequest` union."""
    if cursor is not None and offset is not None:
        raise BadRequestError("supply either ?offset or ?cursor, not both")
    if cursor is not None:
        return CursorPage(cursor=cursor, length=limit)
    return OffsetPage(offset=offset or 0, length=limit)


def parse_order_by(
    order_by: list[str] | None = Query(
        default=None,
        description=(
            "Repeat the parameter for multi-field sort: "
            "``?order_by=name:asc&order_by=id:desc``. Bare ``?order_by=name`` "
            "defaults to ascending."
        ),
    ),
) -> list[OrderBy] | None:
    """Translate ``?order_by=field:direction`` entries into ``list[OrderBy]``."""
    if not order_by:
        return None
    parsed: list[OrderBy] = []
    for entry in order_by:
        if ":" in entry:
            field, _, direction = entry.partition(":")
            if direction not in ("asc", "desc"):
                raise BadRequestError(
                    f"invalid order_by direction {direction!r}; use asc or desc"
                )
        else:
            field, direction = entry, "asc"
        if not field:
            raise BadRequestError("order_by field name must be non-empty")
        parsed.append(OrderBy(field=field, direction=direction))  # type: ignore[arg-type]
    return parsed


class FindRequest(BaseModel):
    """JSON body for ``POST /v1/<resource>/find`` endpoints."""

    predicate: Predicate | None = Field(
        default=None,
        description="Filter predicate; ``null`` lists every entity matching the page.",
    )
    page: PageRequest = Field(
        ...,
        description="Pagination request (offset or cursor; discriminated by `kind`).",
    )
    order_by: list[OrderBy] | None = Field(
        default=None,
        description="Optional sort keys applied left-to-right.",
    )


__all__ = ["FindRequest", "parse_order_by", "parse_page"]
