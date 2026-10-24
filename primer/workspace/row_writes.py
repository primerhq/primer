"""Field-scoped writes of a workspace row (ticket 01a16d72, round 2 of #917).

The routes and tools that change one field of a workspace (its name, its event streaming, terminal access, the reply binding) used to read the
row and write it back whole. A write that read the row before another writer moved its ``phase`` put the old phase back: a rename that read the
row while a delete marked it ``terminating`` and wrote after the failed delete's ``failed`` left it ``terminating`` for ever (the probe skips
such a row), and one that read it ``pending`` before the probe's promotion put ``pending`` back. :func:`write_workspace_fields` writes only the
named fields, in ONE guarded statement against the row's current document, so every other field (the phase, the probe's results, the other
writers' fields) stays as it is.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, get_args

from primer.model.common import dump_for_storage
from primer.model.workspace import Workspace

#: Every phase a workspace row can hold, so a guard on them matches any row (``patch_if`` needs a guard, and may not name ``id``).
WORKSPACE_PHASES: tuple[str, ...] = get_args(Workspace.model_fields["phase"].annotation)


async def write_workspace_fields(
    storage: Any, row: Workspace, changes: dict[str, Any], *, phases: Iterable[str] = WORKSPACE_PHASES,
) -> Workspace | None:
    """Write ``changes`` (field name to new value) to the stored workspace ``row``, and nothing else.

    The values are dumped as the storage layer stores them (``dump_for_storage`` of the changed row, so a nested model or a secret is written
    whole and unmasked, exactly as a whole-document write would have). ``phases`` guards the write on the row's CURRENT phase (every phase
    by default): the answer is ``None`` when the row's phase is not one of them, and the row after the write otherwise. A row deleted since the
    caller read it raises :class:`~primer.model.except_.NotFoundError` from the storage, as a missing row does.
    """
    dumped = dump_for_storage(row.model_copy(update=changes))
    return await storage.patch_if(row.id, {field: dumped[field] for field in changes}, where={"phase": list(phases)})


__all__ = ["WORKSPACE_PHASES", "write_workspace_fields"]
