"""A harness's access, and how what it installed inherits it (access-control P3 Task 6, Decision 1).

A harness's agents, graphs, toolsets and collections carry a COPY of the harness's ``access`` (documents follow their collection, so
they carry none). Sharing the harness (``PUT /v1/harnesses/{id}/access``, ``set_entity_access``) calls :func:`apply_harness_access`
afterwards; sharing an installed row directly is refused (``harness_managed``).
"""

from __future__ import annotations

import logging
from typing import Any

from primer.model.agent import Agent
from primer.model.collection import Collection
from primer.model.graph import Graph
from primer.model.harness import Harness
from primer.model.providers.toolset import Toolset
from primer.model.storage import OffsetPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)

#: The kinds of rows a harness installs that carry ``access``.
INSTALLED_ACCESS_MODELS: tuple[type, ...] = (Agent, Graph, Toolset, Collection)

_PAGE = 200
_ATTEMPTS = 4


async def _copy_to_row(storage: Any, row: Any, harness: Harness, access: dict[str, Any]) -> int:
    """Copy ``harness.access`` onto one row, never over a newer sharing: 1 when written, else 0.

    Ordered by ``access.rev``: a row already at a higher rev than the harness (a later fan-out got there first) is left alone, and the
    write is fenced on the rev just observed, so two fan-outs racing on a row cannot resurrect a revoked grant. A lost fence re-reads the
    row and decides again, a few times at most.
    """
    for _ in range(_ATTEMPTS):
        if row.access.rev > harness.access.rev or row.access == harness.access:
            return 0
        rev = row.access.rev
        where = {"harness_id": [harness.id], ("access", "rev"): [rev, None] if rev == 0 else [rev]}
        if await storage.patch_if(row.id, {"access": access}, where=where) is not None:
            return 1
        row = await storage.get(row.id)
        if row is None or row.harness_id != harness.id:
            return 0
    return 0


async def apply_harness_access(storage_provider: Any, harness: Harness) -> int:
    """Copy ``harness.access`` onto every row installed by ``harness`` (one field-scoped write each, ordered by rev: see :func:`_copy_to_row`); returns how many rows it wrote.

    Only ``access`` is patched, and only while the row still belongs to the harness, so no other field a concurrent writer set is put
    back and a row an uninstall already released is left alone. Re-running converges.
    """
    access = harness.access.model_dump(mode="json")
    written = 0
    for model in INSTALLED_ACCESS_MODELS:
        storage = storage_provider.get_storage(model)
        predicate = Q(model).where("harness_id", harness.id).build()
        offset = 0
        while True:
            page = await storage.find(predicate, OffsetPage(offset=offset, length=_PAGE))
            for row in page.items:
                written += await _copy_to_row(storage, row, harness, access)
            if len(page.items) < _PAGE:
                break
            offset += _PAGE
    if written:
        logger.info("applied a harness's access to what it installed", extra={"harness_id": harness.id, "rows": written})
    return written


__all__ = ["INSTALLED_ACCESS_MODELS", "apply_harness_access"]
