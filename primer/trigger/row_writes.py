"""Field-scoped writes of a stored trigger row (access-control plan P3, Decision 8).

A platform writer that changes some fields of a trigger (a fire's bookkeeping, the claim release's next fire time) writes
those fields alone. A whole-row write of the row as read would put back what another writer changed in between: the sharing
an owner changed (a revoked grant resurrected), or an edit saved while the fire ran.
"""

from __future__ import annotations

from typing import Any

from primer.model.common import dump_for_storage
from primer.model.except_ import ConflictError
from primer.model.trigger import Trigger


async def write_trigger_fields(storage: Any, row: Trigger, changes: dict[str, Any], *, conn: Any | None = None) -> Trigger:
    """Write ``changes`` (field name to new value) to the stored trigger ``row``, and nothing else.

    The values are dumped as the storage layer stores them. ``slug`` never changes after a create (no path writes it), so the
    guard on it always holds; it is there because ``patch_if`` takes one. A row deleted since the caller read it raises
    ``NotFoundError`` from the storage.
    """
    dumped = dump_for_storage(row.model_copy(update=changes))
    written = await storage.patch_if(row.id, {field: dumped[field] for field in changes}, where={"slug": [row.slug]}, conn=conn)
    if written is None:
        raise ConflictError(f"Trigger {row.id!r} changed its slug while it was being written")
    return written


__all__ = ["write_trigger_fields"]
