"""Field-scoped writes of a stored collection row (access-control plan P3, Decision 8).

A platform writer that changes some fields of a collection (the search block, the system wiki's title, an internal
collection's description) writes those fields alone. A whole-row write of the row as read would put back the sharing
an owner changed in between (a revoked grant resurrected).
"""

from __future__ import annotations

from typing import Any

from primer.model.collection import Collection
from primer.model.common import dump_for_storage
from primer.model.except_ import ConflictError


async def write_collection_fields(storage: Any, row: Collection, changes: dict[str, Any]) -> Collection:
    """Write ``changes`` (field name to new value) to the stored collection ``row``, and nothing else.

    The values are dumped as the storage layer stores them (``dump_for_storage`` of the changed row). ``system`` never
    changes after a create (every path refuses it), so the guard on it always holds; it is there because ``patch_if``
    takes one. A row deleted since the caller read it raises ``NotFoundError`` from the storage.
    """
    dumped = dump_for_storage(row.model_copy(update=changes))
    written = await storage.patch_if(row.id, {field: dumped[field] for field in changes}, where={"system": [row.system]})
    if written is None:
        raise ConflictError(f"Collection {row.id!r} changed its system flag while it was being written")
    return written


__all__ = ["write_collection_fields"]
