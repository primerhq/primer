"""Shared helpers and argument models for the ``system`` toolset.

Split out of :mod:`primer.toolset.system` (a god-module decomposition).
Holds the JSON-error wrappers, the reusable Pydantic argument models, and
the page/order-by parsers that both the generic CRUD generators
(:mod:`primer.toolset._system_crud`) and the hand-written system tools
build on. ``system.py`` re-exports the public-facing names it needs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import ToolCallResult
from primer.model.except_ import PrimerError
from primer.model.storage import (
    CursorPage,
    OffsetPage,
    OrderBy,
)
from primer.toolset._helpers import err as _err


logger = logging.getLogger("primer.toolset.system")


SYSTEM_TOOLSET_ID = "system"


# ===========================================================================
# Helpers — JSON encoding + uniform error wrapping
# ===========================================================================


def _err_from_primer(exc: PrimerError, *, error_type: str) -> ToolCallResult:
    return _err(getattr(exc, "message", str(exc)), error_type=error_type)


def _err_from_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(exc.errors(), default=str),
        error_type="validation-error",
    )


# ===========================================================================
# Argument models — shared shapes
# ===========================================================================


class _GetByIdArgs(BaseModel):
    """Look up an entity by its id."""

    id: str = Field(..., min_length=1, description="Entity id (case-sensitive).")


class _DeleteByIdArgs(BaseModel):
    """Delete an entity by its id."""

    id: str = Field(..., min_length=1, description="Entity id (case-sensitive).")


class _PaginationArgs(BaseModel):
    """Page selector — supply EITHER ``offset`` OR ``cursor``, never both."""

    limit: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Maximum number of items returned (1-200, default 20).",
    )
    offset: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Offset-based: number of items to skip. Mutually exclusive "
            "with ``cursor``. If both are omitted, defaults to offset 0."
        ),
    )
    cursor: str | None = Field(
        default=None,
        description=(
            "Cursor-based: opaque cursor returned as ``next_cursor`` by "
            "a prior list call. Mutually exclusive with ``offset``."
        ),
    )
    order_by: list[str] | None = Field(
        default=None,
        description=(
            "Sort spec, e.g. ``['id:asc', 'name:desc']``. Each entry "
            "is ``field:direction`` where direction is ``asc`` or ``desc``. "
            "Direction defaults to ``asc`` if omitted."
        ),
    )


class _FindArgs(_PaginationArgs):
    """Predicate-based search arguments."""

    predicate: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Predicate tree (see :class:`primer.model.storage.Predicate`). "
            "Binary tree of comparison/logical ops. Each node is "
            "``{kind:'predicate', left:..., op:..., right:...}``; leaf "
            "field references are ``{kind:'field', name:'...'}`` and "
            "literal values are ``{kind:'value', value:...}``. "
            "Operators: =, !=, ~=, >, <, >=, <=, in, and, or. "
            "Pass ``null`` to find all rows (equivalent to list)."
        ),
    )


def _parse_page(args: _PaginationArgs) -> OffsetPage | CursorPage:
    if args.offset is not None and args.cursor is not None:
        raise ValueError("supply either ``offset`` or ``cursor``, not both")
    if args.cursor is not None:
        return CursorPage(cursor=args.cursor, length=args.limit)
    return OffsetPage(offset=args.offset or 0, length=args.limit)


def _parse_order_by(spec: list[str] | None) -> list[OrderBy] | None:
    if spec is None:
        return None
    parsed: list[OrderBy] = []
    for entry in spec:
        if ":" in entry:
            field, direction = entry.split(":", 1)
            direction = direction.strip().lower() or "asc"
            if direction not in ("asc", "desc"):
                raise ValueError(
                    f"invalid order_by direction {direction!r} in {entry!r}; "
                    "must be 'asc' or 'desc'"
                )
        else:
            field, direction = entry, "asc"
        parsed.append(OrderBy(field=field.strip(), direction=direction))  # type: ignore[arg-type]
    return parsed
