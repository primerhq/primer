"""One approval gate per operation, whatever the door (audit G6-1).

The seeded approval policies gated the ``crud`` tools only. The same writes have other doors: the system toolset's agent and graph tools share the
crud tools' factory and guards, and the trigger toolset's create / update ARE the handlers crud re-exports; deletes of agents, graphs and triggers
have no crud tool at all. All of them are role user, and ``system__call_tool`` (every seeded agent's door) reaches them, so a model could rewrite any
agent's system prompt with no approval card while its prompt and the wiki said those writes pause for approval.

The table below is every tool that creates, updates or deletes an agent, a graph or a trigger, in every toolset that has one (the completeness test
reads the live listings of those toolsets, so a new door fails it until it is in the table and gated). Each is called directly through the tool
manager and through ``system__call_tool``, on SQLite (whose ``find`` honours the predicate, so a lookup under the wrong key cannot pass), in two
worlds: a FRESH install (the ensure pass seeds every policy) and an UPGRADED one (it has only the nine crud policies the old seed wrote; the boot
runs the migrations, then the ensure pass). Every call must park on the seeded policy of its own key before anything is written.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from primer.agent.approval import ApprovalResolver
from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.bootstrap.seed import crud_policy_id, default_policy_id, ensure_crud_approval_policies
from primer.model.agent import Agent
from primer.model.chat import ToolCallPart
from primer.model.graph import Graph
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.trigger import Trigger
from primer.model.yield_ import YieldToWorker
from primer.storage.migrations import LATEST_VERSION, run_migrations
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.crud import CRUD_TOOL_NAMES, CRUD_TWINS, DEFAULT_GATED_TOOLS, PREVIEW_ARGS, build_crud_toolset
from primer.toolset.system import build_system_toolset
from primer.toolset.tool_catalog import default_gate
from primer.toolset.trigger import build_trigger_toolset_provider
from tests._support.agent_check_rows import agent_body

USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")

AGENT = agent_body("ag-1")
GRAPH = {
    "id": "g-1",
    "description": "a graph",
    "nodes": [{"kind": "begin", "id": "begin"}, {"kind": "end", "id": "end"}],
    "edges": [{"kind": "static", "from_node": "begin", "to_node": "end"}],
}
TRIGGER = {"slug": "nightly", "name": "Nightly", "config": {"kind": "scheduled", "cron": "0 2 * * *"}}

#: ``(toolset_id, tool_name, arguments)``: every create, update and delete of an agent, a graph or a trigger, in every toolset.
WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("crud", "create_agent", {"entity": AGENT}),
    ("crud", "update_agent", {"id": "ag-1", "entity": AGENT}),
    ("crud", "create_graph", {"entity": GRAPH}),
    ("crud", "update_graph", {"id": "g-1", "entity": GRAPH}),
    ("crud", "create_trigger", TRIGGER),
    ("crud", "update_trigger", {"id": "t-1", "name": "renamed"}),
    ("system", "create_agent", {"entity": AGENT}),
    ("system", "update_agent", {"id": "ag-1", "entity": AGENT}),
    ("system", "delete_agent", {"id": "ag-1"}),
    ("system", "create_graph", {"entity": GRAPH}),
    ("system", "update_graph", {"id": "g-1", "entity": GRAPH}),
    ("system", "delete_graph", {"id": "g-1"}),
    ("trigger", "create", TRIGGER),
    ("trigger", "update", {"id": "t-1", "name": "renamed"}),
    ("trigger", "delete", {"id": "t-1"}),
]

# A tool name that writes one of the three entities: ``<op>_<entity>`` anywhere, or a bare ``<op>`` in the trigger toolset (its entity is the trigger).
_WRITE_NAME = re.compile(r"^(create|update|delete)_(agent|graph|trigger)$")


def _page(length: int) -> OffsetPage:
    return OffsetPage(offset=0, length=length)


class _Session:
    session_id = "sess-1"
    workspace_id = "ws-1"
    agent_id = "agent-builder"
    workspace_tools: list = []


async def _world(tmp_path: Path, kind: str):
    """The storage, registry, toolsets and resolver of an install that is ``fresh`` or ``upgraded`` (see the module docstring)."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / f"{kind}.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    if kind == "fresh":
        await ensure_crud_approval_policies(sp)
    else:
        # What the seed before the fix wrote: one required policy per crud tool and nothing else. The install is at schema version 12, the last
        # version before the twins were gated, and its boot runs the migrations and then the ensure pass, in that order.
        store = sp.get_storage(ToolApprovalPolicy)
        for name in CRUD_TOOL_NAMES:
            await store.create(ToolApprovalPolicy(id=crud_policy_id(name), toolset_id="crud", tool_name=name, approval=RequiredApprovalConfig()))
        await sp.set_bootstrap_completed(datetime.now(UTC))
        await sp.set_schema_version(12)
        assert await run_migrations(sp, is_fresh_install=False) == LATEST_VERSION
        await ensure_crud_approval_policies(sp)
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )
    resolver = ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy))
    system = build_system_toolset(storage_provider=sp, provider_registry=registry, approval_resolver=resolver)
    crud = build_crud_toolset(storage_provider=sp)
    trigger = build_trigger_toolset_provider(storage_provider=sp)
    registry._system_toolset_provider = system  # type: ignore[attr-defined]
    registry._crud_toolset_provider = crud  # type: ignore[attr-defined]
    registry._trigger_toolset_provider = trigger  # type: ignore[attr-defined]
    return sp, registry, {"system": system, "crud": crud, "trigger": trigger}, resolver


@pytest.mark.parametrize("door", ["direct", "through-call-tool"])
@pytest.mark.parametrize("toolset_id, tool_name, arguments", WRITES, ids=[f"{t}__{n}" for t, n, _ in WRITES])
@pytest.mark.parametrize("kind", ["fresh", "upgraded"])
async def test_every_door_to_a_platform_write_parks_on_its_seeded_gate(tmp_path, kind, toolset_id, tool_name, arguments, door):
    sp, registry, toolsets, resolver = await _world(tmp_path, kind)
    try:
        manager = ToolExecutionManager.for_workspace(
            toolset_providers=toolsets, session=_Session(), approval_resolver=resolver, provider_registry=registry,
            tools=["system__call_tool", *(f"{t}__{n}" for t, n, _ in WRITES)], initiated_by=USER,
        )
        if door == "direct":
            call = ToolCallPart(id="c1", name=f"{toolset_id}__{tool_name}", arguments=arguments)
        else:
            call = ToolCallPart(id="c1", name="system__call_tool", arguments={"toolset_id": toolset_id, "tool_name": tool_name, "arguments": arguments})

        with pytest.raises(YieldToWorker) as parked:
            await manager.execute(call)

        meta = parked.value.yielded.resume_metadata
        assert parked.value.yielded.tool_name == "_approval"
        assert meta["policy_id"] == default_policy_id(toolset_id, tool_name), meta
        assert meta["approval_type"] == "required"
        if door == "through-call-tool":
            assert meta["via_call_tool"] == {"toolset_id": toolset_id, "tool_name": tool_name}
        for model in (Agent, Graph, Trigger):
            assert (await sp.get_storage(model).list(_page(10))).items == [], f"{model.__name__} written before the gate"
    finally:
        await sp.aclose()


async def test_the_table_is_every_write_of_an_agent_graph_or_trigger_in_those_toolsets(tmp_path):
    """A new tool that creates, updates or deletes one of the three entities in these toolsets fails here until it is in the table (and so gated)."""
    sp, _registry, toolsets, _resolver = await _world(tmp_path, "fresh")
    try:
        found: set[tuple[str, str]] = set()
        for toolset_id, provider in toolsets.items():
            async for tool in provider.list_tools():
                if _WRITE_NAME.match(tool.id) or (toolset_id == "trigger" and tool.id in {"create", "update", "delete"}):
                    found.add((toolset_id, tool.id))
        assert found == {(t, n) for t, n, _ in WRITES}
        assert found <= set(DEFAULT_GATED_TOOLS)
    finally:
        await sp.aclose()


async def test_both_worlds_end_with_the_same_gates(tmp_path):
    """An upgraded install that never touched its policies holds exactly the rows a fresh install is seeded with."""
    shapes = {}
    for kind in ("fresh", "upgraded"):
        sp, *_ = await _world(tmp_path, kind)
        try:
            page = await sp.get_storage(ToolApprovalPolicy).list(_page(100))
            shapes[kind] = sorted((p.id, p.toolset_id, p.tool_name, p.enabled, p.approval.type.value) for p in page.items)
        finally:
            await sp.aclose()
    assert shapes["fresh"] == shapes["upgraded"]
    assert {(t, n) for _, t, n, _, _ in shapes["fresh"]} == set(DEFAULT_GATED_TOOLS)


def test_the_shipped_gate_of_every_twin_is_required():
    """The reference pages and ``describe_tool``'s callers read the shipped gate from ``default_gate``: the twins ship gated like the crud tools."""
    assert all(default_gate(t, n) == "required" for t, n in DEFAULT_GATED_TOOLS)
    assert default_gate("system", "get_agent") is None and default_gate("trigger", "list") is None


async def test_every_twin_card_shows_what_its_crud_siblings_card_shows(tmp_path):
    """A default-gated tool declares what its approval card may show (``tests/toolset/test_crud_preview_declarations.py`` pins the crud tools): each
    twin shows its crud sibling's paths, and a delete shows the id it removes."""
    sp, _registry, toolsets, _resolver = await _world(tmp_path, "fresh")
    try:
        for (toolset_id, tool_name), sibling in CRUD_TWINS.items():
            tool = {t.id: t async for t in toolsets[toolset_id].list_tools()}[tool_name]
            expected = PREVIEW_ARGS[sibling] if sibling is not None else ("id",)
            assert tool.preview_args == expected, (toolset_id, tool_name, tool.preview_args)
    finally:
        await sp.aclose()
