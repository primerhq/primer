"""The one query behind a reference-integrity block.

A parent entity must not be deleted while a child row still references it. The REST routers
(:mod:`primer.api.routers._references`) and the system CRUD tools (:mod:`primer.toolset._system_guards`) both ask the same
question, "is there at least one row of the child kind whose field equals, or contains, the parent id", so the query lives
here once and neither surface can drift from the other on how a reference is looked for.
"""

from __future__ import annotations

from typing import Any

from primer.model.storage import FieldRef, OffsetPage, Op, Predicate, Value


async def first_referencing_row(storage: Any, *, field: str, op: Op, parent_id: str) -> Any | None:
    """Return one child row whose ``field`` matches ``parent_id`` under ``op``, or ``None`` when nothing references it.

    ``op`` is :attr:`~primer.model.storage.Op.EQ` for a plain foreign-key field and ``Op.CONTAINS`` for a JSON array field
    (an aggregate profile's ``members``). Only one row is fetched: callers treat the answer as "at least one child exists".
    """
    page = await storage.find(
        Predicate(left=FieldRef(name=field), op=op, right=Value(value=parent_id)),
        OffsetPage(offset=0, length=1),
    )
    return page.items[0] if page.items else None


__all__ = ["first_referencing_row"]
