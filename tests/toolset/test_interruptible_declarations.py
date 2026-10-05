"""Which system tools a Stop must NOT cancel (stop slice B1), pinned in both directions.

A tool is declared ``interruptible=False`` only after its handler was READ and shown to do one of two things: perform two
or more durable writes across stores or rows with no single transaction, or write inside a lock that outlives the await
(cancelling the await releases the lock while the write still runs). When unsure a mutator is NOT interruptible: the
cost is a Stop that waits a few seconds, the opposite mistake leaves a half-done write.

The set below is what was confirmed by reading, and why:

* ``collections`` create/update/move/delete_document: a DB transaction (document and content rows), then a SEPARATE
  step on another store (the indexer, the chunk path rewriter, the un-indexer, each looped per descendant). A cancel in
  between leaves the index stale; for a delete, chunks search can still return for pages that no longer exist.
* ``system__put_document``: ``DocumentService.upsert``, a transaction then the indexer.
* ``workspaces`` write_workspace_file / delete_workspace_file: ``LocalWorkspace`` writes in a thread under the scope lock
  (the same hazard as the ``write`` / ``edit`` tools).

Examined and left interruptible (one atomic write, or none): ``system__move_document`` and the document delete behind
``DocumentService`` (one transaction), ``refresh_collection`` (a not-implemented stub), and ``create_python_toolset`` /
``update_python_toolset_source`` (one storage write each; built outside this registry, so not enumerated here). Not yet
examined: see the follow-up ticket.

The registry fixture is the one ``tests/mcp/test_required_role_completeness.py`` builds (every reserved built-in toolset,
wired as ``primer/api/_app_lifespan.py`` does), so a new reserved toolset cannot fall outside this test.
"""

from __future__ import annotations

import pytest

from primer.api.registries import ProviderRegistry
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.toolset._system_common import SYSTEM_TOOLSET_ID
from primer.toolset.collections import COLLECTIONS_TOOLSET_ID
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID
from tests.mcp.test_required_role_completeness import reserved_provider_registry  # noqa: F401  (the fixture)

CONFIRMED_NOT_INTERRUPTIBLE = {
    (COLLECTIONS_TOOLSET_ID, "create_document"),
    (COLLECTIONS_TOOLSET_ID, "update_document"),
    (COLLECTIONS_TOOLSET_ID, "move_document"),
    (COLLECTIONS_TOOLSET_ID, "delete_document"),
    (SYSTEM_TOOLSET_ID, "put_document"),
    (WORKSPACES_TOOLSET_ID, "write_workspace_file"),
    (WORKSPACES_TOOLSET_ID, "delete_workspace_file"),
}

EXAMINED_AND_INTERRUPTIBLE = {
    (SYSTEM_TOOLSET_ID, "move_document"),
    (SYSTEM_TOOLSET_ID, "refresh_collection"),
    (COLLECTIONS_TOOLSET_ID, "read_document"),
    (WORKSPACES_TOOLSET_ID, "read_workspace_file"),
}


async def _declared(registry: ProviderRegistry) -> dict[tuple[str, str], bool]:
    out: dict[tuple[str, str], bool] = {}
    for toolset_id in RESERVED_TOOLSET_IDS:
        provider = await registry.get_toolset(toolset_id)
        async for tool in provider.list_tools():
            out[(tool.toolset_id, tool.id)] = tool.interruptible
    return out


@pytest.mark.asyncio
async def test_the_non_interruptible_system_tools_are_exactly_the_confirmed_ones(
    reserved_provider_registry: ProviderRegistry,  # noqa: F811
) -> None:
    declared = await _declared(reserved_provider_registry)

    not_interruptible = {key for key, interruptible in declared.items() if not interruptible}

    assert not_interruptible == CONFIRMED_NOT_INTERRUPTIBLE, (
        "the set of tools a Stop must not cancel changed. Declare interruptible=False only after reading the handler "
        "(two or more durable writes with no single transaction, or a write in a lock that outlives the await), and "
        f"add the tool to this test with the reason. Unexpected: {sorted(not_interruptible - CONFIRMED_NOT_INTERRUPTIBLE)}; "
        f"missing: {sorted(CONFIRMED_NOT_INTERRUPTIBLE - not_interruptible)}"
    )


@pytest.mark.asyncio
async def test_every_confirmed_tool_exists_and_the_examined_neighbours_are_interruptible(
    reserved_provider_registry: ProviderRegistry,  # noqa: F811
) -> None:
    declared = await _declared(reserved_provider_registry)

    assert CONFIRMED_NOT_INTERRUPTIBLE <= set(declared), "a confirmed tool was renamed or removed"
    assert EXAMINED_AND_INTERRUPTIBLE <= set(declared), "an examined tool was renamed or removed"
    assert all(declared[key] for key in EXAMINED_AND_INTERRUPTIBLE)
