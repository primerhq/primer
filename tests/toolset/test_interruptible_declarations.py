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
* ``system__create_document`` / ``system__delete_document`` (since task 01a111d1 D3 they delegate to ``DocumentTreeService``):
  a transaction over the entity and content rows, then the indexer (create) or the unindexer (delete) on another store, the
  same shape as the collections toolset's document tools. ``update_document`` stays interruptible: it changes title and meta
  on one row (a path, slug, parent or collection change is refused).
* ``workspaces`` write_workspace_file / delete_workspace_file: ``LocalWorkspace`` writes in a thread under the scope lock
  (the same hazard as the ``write`` / ``edit`` tools).

The second review (task 01a10e2f; the lead decided the list) added:

* ``trigger`` create / update / delete / fire_now, and the ``crud`` copies create_trigger / update_trigger (``model_copy``
  copies of the same Tool objects, which keep the flag): create and update write the trigger row and THEN upsert its claim lease on another connection; a cancel between
  leaves a row that never fires, and nothing re-creates the lease. delete loops one delete per subscription and then deletes
  the trigger: a cancel leaves a live trigger with some subscriptions gone. fire_now fans out (resume a parked session,
  delete its subscription, per subscription) and then updates the trigger: a cancel delivers to some, and a retry delivers
  again.
* ``workspaces`` create_workspace / delete_workspace: provisioning (or tearing down) a container/pod/volume and the row are
  two separate steps, a cancel between leaks the instance or leaves a ghost row. The tools ALSO carry the REST route's
  ``except BaseException`` rollback (create) and run the teardown shielded (delete): the flag governs a Stop only (past the
  5 s grace the call is abandoned and runs on), while a hard Cancel of the turn cancels every call whatever it declares.
* ``workspaces`` create / cancel / steer / restart_workspace_session: several writes (state repo commit under the commit
  lock, session row, enqueue, claim lease) with no transaction; a cancel can leave a RUNNING session with no lease (the
  sweeper ends it only after 600 s), a ``last_seq`` behind the log, or a second divider with the same seq.
* ``system`` update_ / delete_ of llm_provider, embedding_provider, cross_encoder_provider, toolset,
  semantic_search_provider and model_profile (the six entities that carry an invalidation hook; model_profile joined them with
  task 01a111d1 D5, when its aggregated-LLM invalidation was wired as the REST router has it): the row is written and THEN the
  registry cache is evicted, with no transaction between; a cancel between leaves a deleted provider still serving, or a rotated
  key not picked up, until a restart. (A cache eviction is not a durable write; this is the "when unsure" side of the rule.)
* ``crud`` update_python_toolset_source (task 01a111d1 D5 2a, when it gained the registry eviction the toolset router has on
  update): the toolset row is written and THEN the registry's cached adapter is evicted, the same shape as the six entities
  above; a cancel between leaves the old source serving until a restart.

The third review (task 01a111d1, D1) added:

* ``harness`` fetch / install / sync / uninstall: write ``pending_operation`` on the harness row and THEN upsert its claim
  lease (``announce_enqueued``), two writes with no transaction; a cancel between leaves an operation pending that nothing
  will claim, which blocks every later operation on that harness with a conflict (the defect that task fixed, now
  not re-created by a cancel). The first review examined ``harness__install`` as one row write and left it interruptible;
  that was true only while the toolset did not upsert the lease.

``call_tool`` is not in the list on purpose: it is a pass-through, and the loop decides interruptibility for the tool it
WRAPS (``ToolExecutionManager.is_interruptible_call``), so ``call_tool`` of a tool above is not cancelled either.

Examined and left interruptible (one atomic write, or none): ``system__move_document`` and the document delete behind
``DocumentService`` (one transaction), ``refresh_collection`` (a not-implemented stub), and the ``crud`` toolset's
``create_python_toolset`` (registration is in-process parsing, then one storage write; nothing is cached yet); every
other single-row create / update / delete (the other ten system entities, agent, graph, subscriptions, workspace providers
and templates, the harness register / update / update_overrides tools: one row write in one transaction); ``invoke_agent`` (a cancel unwinds the subagent, and a
subagent that swallows it is stopped by the Stop event it is given; the record flush a cancel could interrupt was made
cancel-safe in the message writer, task D4); ``invoke_graph`` (a cancel DOES propagate to the in-process child graph, whose
node tasks are cancelled and awaited; flagging it non-interruptible would instead abandon the call after the grace and
leave the child running ownerless, because the graph executor reads no Stop event); ``workspace_tap`` (its cursor is held
by the client, so a cancel loses no events).

The registry fixture is the one ``tests/mcp/test_required_role_completeness.py`` builds (every reserved built-in toolset,
wired as ``primer/api/_app_lifespan.py`` does), so a new reserved toolset cannot fall outside this test.
"""

from __future__ import annotations

import pytest

from primer.api.registries import ProviderRegistry
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.toolset._system_common import SYSTEM_TOOLSET_ID
from primer.toolset.collections import COLLECTIONS_TOOLSET_ID
from primer.toolset.crud import CRUD_TOOLSET_ID
from primer.toolset.harness import HARNESS_TOOLSET_ID
from primer.toolset.trigger import TRIGGER_TOOLSET_ID
from primer.toolset.workspace_ext import WORKSPACE_EXT_TOOLSET_ID
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID
from tests.mcp.test_required_role_completeness import reserved_provider_registry  # noqa: F401  (the fixture)

CONFIRMED_NOT_INTERRUPTIBLE = {
    (COLLECTIONS_TOOLSET_ID, "create_document"),
    (COLLECTIONS_TOOLSET_ID, "update_document"),
    (COLLECTIONS_TOOLSET_ID, "move_document"),
    (COLLECTIONS_TOOLSET_ID, "delete_document"),
    (SYSTEM_TOOLSET_ID, "put_document"),
    # task 01a111d1 (D3): delegate to DocumentTreeService: a transaction (entity + content), then the indexer / unindexer
    (SYSTEM_TOOLSET_ID, "create_document"),
    (SYSTEM_TOOLSET_ID, "delete_document"),
    (WORKSPACES_TOOLSET_ID, "write_workspace_file"),
    (WORKSPACES_TOOLSET_ID, "delete_workspace_file"),
    # task 01a10e2f: triggers (row then lease, per-subscription loops, fan-out)
    (TRIGGER_TOOLSET_ID, "create"),
    (TRIGGER_TOOLSET_ID, "update"),
    (TRIGGER_TOOLSET_ID, "delete"),
    (TRIGGER_TOOLSET_ID, "fire_now"),
    (CRUD_TOOLSET_ID, "create_trigger"),
    (CRUD_TOOLSET_ID, "update_trigger"),
    # task 01a10e2f: workspace and session lifecycle (a resource and a row, or several writes)
    (WORKSPACES_TOOLSET_ID, "create_workspace"),
    (WORKSPACES_TOOLSET_ID, "delete_workspace"),
    (WORKSPACES_TOOLSET_ID, "create_workspace_session"),
    (WORKSPACES_TOOLSET_ID, "cancel_workspace_session"),
    (WORKSPACES_TOOLSET_ID, "steer_workspace_session"),
    (WORKSPACES_TOOLSET_ID, "restart_workspace_session"),
    # task 01a10e2f (+ model_profile, task 01a111d1 D5): a row write THEN a registry cache eviction (the entities that carry an
    # invalidation hook)
    *{
        (SYSTEM_TOOLSET_ID, f"{verb}_{entity}")
        for verb in ("update", "delete")
        for entity in (
            "llm_provider", "embedding_provider", "cross_encoder_provider", "toolset", "semantic_search_provider",
            "model_profile",
        )
    },
    # task 01a111d1 (D5 2a): the toolset row, then the registry's cached adapter evicted
    (CRUD_TOOLSET_ID, "update_python_toolset_source"),
    # task 01a111d1 (D1): the row, then the claim lease (a separate write on another connection)
    (HARNESS_TOOLSET_ID, "harness__fetch"),
    (HARNESS_TOOLSET_ID, "harness__install"),
    (HARNESS_TOOLSET_ID, "harness__sync"),
    (HARNESS_TOOLSET_ID, "harness__uninstall"),
}

EXAMINED_AND_INTERRUPTIBLE = {
    (SYSTEM_TOOLSET_ID, "move_document"),
    (SYSTEM_TOOLSET_ID, "refresh_collection"),
    (COLLECTIONS_TOOLSET_ID, "read_document"),
    (WORKSPACES_TOOLSET_ID, "read_workspace_file"),
    # task 01a10e2f: decided to stay interruptible, with the reason in the docstring above
    (SYSTEM_TOOLSET_ID, "invoke_agent"),
    (WORKSPACE_EXT_TOOLSET_ID, "invoke_graph"),
    (WORKSPACES_TOOLSET_ID, "workspace_tap"),
    (SYSTEM_TOOLSET_ID, "create_agent"),
    (SYSTEM_TOOLSET_ID, "delete_channel"),
    (TRIGGER_TOOLSET_ID, "create_subscription"),
    (TRIGGER_TOOLSET_ID, "delete_subscription"),
    (CRUD_TOOLSET_ID, "create_agent"),
    (CRUD_TOOLSET_ID, "create_python_toolset"),
    (WORKSPACES_TOOLSET_ID, "create_workspace_template"),
    (WORKSPACES_TOOLSET_ID, "delete_workspace_provider"),
    (WORKSPACES_TOOLSET_ID, "pause_workspace_session"),
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
