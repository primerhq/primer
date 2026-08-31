"""Migration 6: grant rewrite for the unified search tool.

``collections__grep_collection`` and ``collections__semantic_search``
retired in favour of ``collections__search``. Rows written before the
cutover still grant the old ids; this rewrites them in place, preserving
order and deduplicating when an agent carried both.

The view redefines ``Agent`` (``extra="allow"``) for the usual reason: a
migration must read rows the live model may reject and must not depend
on fields it does not touch. See m002/m003 for the idiom.
"""

from __future__ import annotations

import logging

from pydantic import ConfigDict, Field

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.storage.migrations.m003_session_cutover import _iter_rows

logger = logging.getLogger(__name__)

_OLD = {"collections__grep_collection", "collections__semantic_search"}
_NEW = "collections__search"


class Agent(Identifiable):  # noqa: N801 - name selects the storage table
    """Migration-local view: id plus the grant list, everything else rides."""

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    tools: list[str] = Field(default_factory=list)


class M006UnifiedSearchGrants:
    """Point every agent's grant at the one search tool."""

    version = 6
    description = "rewrite agent grants from grep/semantic to unified search"

    async def apply(self, sp: StorageProvider) -> None:
        agents = sp.get_storage(Agent)
        rewritten = 0
        async for row in _iter_rows(agents):
            if not any(t in _OLD for t in row.tools):
                continue
            out: list[str] = []
            for t in row.tools:
                repl = _NEW if t in _OLD else t
                if repl not in out:
                    out.append(repl)
            await agents.update(row.model_copy(update={"tools": out}))
            rewritten += 1
        if rewritten:
            logger.info("rewrote search grants", extra={"count": rewritten})


__all__ = ["M006UnifiedSearchGrants"]
