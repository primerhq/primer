"""Tests for matrix.harness.service — cross-ref rewriting + apply orchestrators."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from primer.harness.hashes import hash_rendered_payload, hash_template_source
from primer.harness.service import (
    BuildErrors,
    apply_install,
    apply_sync,
    apply_uninstall,
    build_rendered_entries,
    resolved_id,
)
from primer.harness.template import RenderedFile
from primer.model.harness import Harness, HarnessRendering, HarnessStatus, RenderedEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_harness(slug: str = "acme") -> Harness:
    return Harness(
        id=f"{slug}-id",
        slug=slug,
        name="Acme",
        git_url="https://github.com/x/y",
        created_at=datetime.now(timezone.utc),
    )


def _make_rendered_file(
    kind: str,
    name: str,
    spec: dict[str, Any],
    content: str | None = None,
) -> RenderedFile:
    source = f"kind: {kind}\nname: {name}\nspec: ...".encode()
    return RenderedFile(
        template_path=f"{name}.yaml",
        template_name=name,
        kind=kind,
        source_bytes=source,
        rendered_text="",
        rendered={"kind": kind, "name": name, "spec": spec},
        content=content,
    )


# ---------------------------------------------------------------------------
# 1. resolved_id
# ---------------------------------------------------------------------------


def test_resolved_id_basic():
    assert resolved_id("acme", "assistant") == "acme__assistant"


def test_resolved_id_with_hyphens():
    assert resolved_id("my-harness", "my-agent") == "my-harness__my-agent"


# ---------------------------------------------------------------------------
# 2. build_rendered_entries — Agent tools cross-ref rewrite
# ---------------------------------------------------------------------------


def test_build_rewrites_agent_tools_known_toolset():
    """Agent.tools whose toolset_id matches a harness toolset template_name → rewritten."""
    toolset_file = _make_rendered_file(
        "toolset", "my-toolset",
        {"provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["cmd"]}}},
    )
    agent_file = _make_rendered_file(
        "agent", "assistant",
        {
            "description": "test",
            "model": {"provider_id": "p", "model_name": "m"},
            "tools": ["my-toolset__hello", "my-toolset__world"],
        },
    )

    entries, errors = build_rendered_entries([toolset_file, agent_file], slug="acme")

    assert not errors
    agent_entry = next(e for e in entries if e.kind == "agent")
    assert "acme__my-toolset__hello" in agent_entry.rendered_payload["tools"]
    assert "acme__my-toolset__world" in agent_entry.rendered_payload["tools"]


def test_build_leaves_external_toolset_tools_alone():
    """Agent.tools referencing a toolset NOT in the harness → left as-is."""
    agent_file = _make_rendered_file(
        "agent", "assistant",
        {
            "description": "test",
            "model": {"provider_id": "p", "model_name": "m"},
            "tools": ["external-toolset__hello"],
        },
    )

    entries, errors = build_rendered_entries([agent_file], slug="acme")

    assert not errors
    agent_entry = entries[0]
    # external-toolset is not a harness template_name → unchanged
    assert "external-toolset__hello" in agent_entry.rendered_payload["tools"]
    assert "acme__external-toolset__hello" not in agent_entry.rendered_payload["tools"]


# ---------------------------------------------------------------------------
# 3. build_rendered_entries — Graph nodes cross-ref rewrite
# ---------------------------------------------------------------------------


def test_build_rewrites_graph_node_agent_id():
    """Graph node agent_id matching a harness agent template_name → rewritten."""
    agent_file = _make_rendered_file(
        "agent", "my-agent",
        {
            "description": "test",
            "model": {"provider_id": "p", "model_name": "m"},
        },
    )
    graph_file = _make_rendered_file(
        "graph", "my-graph",
        {
            "description": "graph",
            "nodes": [
                {"kind": "agent", "id": "n1", "agent_id": "my-agent"},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [{"kind": "static", "from_node": "n1", "to_node": "end"}],
            "entry_node_id": "n1",
        },
    )

    entries, errors = build_rendered_entries([agent_file, graph_file], slug="acme")

    assert not errors
    graph_entry = next(e for e in entries if e.kind == "graph")
    agent_node = next(n for n in graph_entry.rendered_payload["nodes"] if n.get("kind") == "agent")
    assert agent_node["agent_id"] == "acme__my-agent"


def test_build_rewrites_graph_node_graph_id():
    """Graph sub-graph node's graph_id matching a harness graph template_name → rewritten."""
    sub_graph_file = _make_rendered_file(
        "graph", "sub-graph",
        {
            "description": "sub",
            "nodes": [{"kind": "terminal", "id": "t"}],
            "edges": [],
            "entry_node_id": "t",
        },
    )
    main_graph_file = _make_rendered_file(
        "graph", "main-graph",
        {
            "description": "main",
            "nodes": [
                {"kind": "graph", "id": "sg1", "graph_id": "sub-graph"},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [{"kind": "static", "from_node": "sg1", "to_node": "end"}],
            "entry_node_id": "sg1",
        },
    )

    entries, errors = build_rendered_entries([sub_graph_file, main_graph_file], slug="acme")

    assert not errors
    main_entry = next(e for e in entries if e.template_name == "main-graph")
    graph_node = next(n for n in main_entry.rendered_payload["nodes"] if n.get("kind") == "graph")
    assert graph_node["graph_id"] == "acme__sub-graph"


def test_build_leaves_external_graph_id_alone():
    """Graph node graph_id NOT in harness → left as-is."""
    graph_file = _make_rendered_file(
        "graph", "my-graph",
        {
            "description": "graph",
            "nodes": [
                {"kind": "graph", "id": "sg1", "graph_id": "external-graph"},
                {"kind": "terminal", "id": "end"},
            ],
            "edges": [{"kind": "static", "from_node": "sg1", "to_node": "end"}],
            "entry_node_id": "sg1",
        },
    )

    entries, errors = build_rendered_entries([graph_file], slug="acme")

    assert not errors
    graph_entry = entries[0]
    graph_node = next(n for n in graph_entry.rendered_payload["nodes"] if n.get("kind") == "graph")
    assert graph_node["graph_id"] == "external-graph"


# ---------------------------------------------------------------------------
# 4. build_rendered_entries — Document collection_id rewrite
# ---------------------------------------------------------------------------


def test_build_rewrites_document_collection_id():
    """Document.collection_id matching a harness collection template_name → rewritten."""
    collection_file = _make_rendered_file(
        "collection", "docs",
        {
            "description": "docs",
            "embedder": {"provider_id": "ep", "model": "text-emb"},
            "search_provider_id": "ssp",
        },
    )
    doc_file = _make_rendered_file(
        "document", "onboarding",
        {
            "collection_id": "docs",
            "name": "Onboarding",
            "meta": {},
        },
    )

    entries, errors = build_rendered_entries([collection_file, doc_file], slug="acme")

    assert not errors
    doc_entry = next(e for e in entries if e.kind == "document")
    assert doc_entry.rendered_payload["collection_id"] == "acme__docs"


def test_build_leaves_external_collection_id_alone():
    """Document.collection_id NOT in harness → left as-is."""
    doc_file = _make_rendered_file(
        "document", "onboarding",
        {
            "collection_id": "external-collection",
            "name": "Onboarding",
            "meta": {},
        },
    )

    entries, errors = build_rendered_entries([doc_file], slug="acme")

    assert not errors
    doc_entry = entries[0]
    assert doc_entry.rendered_payload["collection_id"] == "external-collection"


# ---------------------------------------------------------------------------
# 5. build_rendered_entries — Pydantic validation errors
# ---------------------------------------------------------------------------


def test_build_collects_pydantic_errors_missing_required():
    """Invalid Agent payload (missing required 'model' field) surfaces in BuildErrors."""
    bad_agent = _make_rendered_file(
        "agent", "bad-agent",
        {
            "description": "missing model",
            # Intentionally omitting required 'model' field
        },
    )

    entries, errors = build_rendered_entries([bad_agent], slug="acme")

    assert bool(errors)
    assert entries == []
    assert len(errors.errors) == 1
    err = errors.errors[0]
    assert err["template_name"] == "bad-agent"
    assert err["kind"] == "agent"
    assert "code" in err
    assert "message" in err


def test_build_collects_multiple_pydantic_errors():
    """Multiple invalid templates → all errors collected, entries empty."""
    bad1 = _make_rendered_file("agent", "bad-1", {"description": "no model"})
    bad2 = _make_rendered_file("agent", "bad-2", {"description": "no model either"})

    entries, errors = build_rendered_entries([bad1, bad2], slug="acme")

    assert bool(errors)
    assert entries == []
    assert len(errors.errors) == 2


def test_build_returns_entries_on_success():
    """Valid payload → entries returned, no errors."""
    agent_file = _make_rendered_file(
        "agent", "assistant",
        {
            "description": "A helpful assistant",
            "model": {"provider_id": "p", "model_name": "m"},
        },
    )

    entries, errors = build_rendered_entries([agent_file], slug="acme")

    assert not errors
    assert len(entries) == 1
    e = entries[0]
    assert e.kind == "agent"
    assert e.template_name == "assistant"
    assert e.resolved_id == "acme__assistant"


# ---------------------------------------------------------------------------
# 6. apply_install — ordering + harness_id stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_install_orders_kinds(fake_storage_provider):
    """install creates entities in toolset → collection → document → agent → graph order."""
    harness = _make_harness("acme")

    toolset_entry = RenderedEntry(
        kind="toolset", template_name="ts",
        resolved_id="acme__ts",
        template_source_hash="h", rendered_hash="h1",
        rendered_payload={"provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["cmd"]}}},
    )
    collection_entry = RenderedEntry(
        kind="collection", template_name="col",
        resolved_id="acme__col",
        template_source_hash="h", rendered_hash="h2",
        rendered_payload={
            "description": "col",
            "embedder": {"provider_id": "ep", "model": "text-emb"},
            "search_provider_id": "ssp",
        },
    )
    doc_entry = RenderedEntry(
        kind="document", template_name="doc",
        resolved_id="acme__doc",
        template_source_hash="h", rendered_hash="h3",
        rendered_payload={"collection_id": "acme__col", "name": "Doc", "meta": {}},
    )
    agent_entry = RenderedEntry(
        kind="agent", template_name="asst",
        resolved_id="acme__asst",
        template_source_hash="h", rendered_hash="h4",
        rendered_payload={
            "description": "assistant",
            "model": {"provider_id": "p", "model_name": "m"},
        },
    )
    graph_entry = RenderedEntry(
        kind="graph", template_name="wf",
        resolved_id="acme__wf",
        template_source_hash="h", rendered_hash="h5",
        rendered_payload={
            "description": "workflow",
            "nodes": [{"kind": "terminal", "id": "t"}],
            "edges": [],
            "entry_node_id": "t",
        },
    )

    entries = [graph_entry, agent_entry, doc_entry, collection_entry, toolset_entry]

    created_kinds: list[str] = []

    from primer.model.provider import Toolset
    from primer.model.collection import Collection, Document
    from primer.model.agent import Agent
    from primer.model.graph import Graph

    original_create_toolset = fake_storage_provider.get_storage(Toolset).create
    original_create_collection = fake_storage_provider.get_storage(Collection).create
    original_create_document = fake_storage_provider.get_storage(Document).create
    original_create_agent = fake_storage_provider.get_storage(Agent).create
    original_create_graph = fake_storage_provider.get_storage(Graph).create

    async def track_toolset(entity):
        created_kinds.append("toolset")
        return await original_create_toolset(entity)

    async def track_collection(entity):
        created_kinds.append("collection")
        return await original_create_collection(entity)

    async def track_document(entity):
        created_kinds.append("document")
        return await original_create_document(entity)

    async def track_agent(entity):
        created_kinds.append("agent")
        return await original_create_agent(entity)

    async def track_graph(entity):
        created_kinds.append("graph")
        return await original_create_graph(entity)

    fake_storage_provider.get_storage(Toolset).create = track_toolset
    fake_storage_provider.get_storage(Collection).create = track_collection
    fake_storage_provider.get_storage(Document).create = track_document
    fake_storage_provider.get_storage(Agent).create = track_agent
    fake_storage_provider.get_storage(Graph).create = track_graph

    rendered_files_by_name: dict[str, RenderedFile] = {}

    error = await apply_install(
        storage_provider=fake_storage_provider,
        harness=harness,
        entries=entries,
        rendered_files_by_name=rendered_files_by_name,
        bundle_hash="bh1",
        overrides_hash="oh1",
        schema_hash=None,
    )

    assert error is None
    assert created_kinds == ["toolset", "collection", "document", "agent", "graph"]


@pytest.mark.asyncio
async def test_apply_install_sets_harness_id_on_entities(fake_storage_provider):
    """Installed entities have harness_id set to harness.id."""
    harness = _make_harness("acme")

    agent_entry = RenderedEntry(
        kind="agent", template_name="asst",
        resolved_id="acme__asst",
        template_source_hash="h", rendered_hash="h1",
        rendered_payload={
            "description": "assistant",
            "model": {"provider_id": "p", "model_name": "m"},
        },
    )

    error = await apply_install(
        storage_provider=fake_storage_provider,
        harness=harness,
        entries=[agent_entry],
        rendered_files_by_name={},
        bundle_hash="bh1",
        overrides_hash="oh1",
        schema_hash=None,
    )

    assert error is None
    from primer.model.agent import Agent
    stored = await fake_storage_provider.get_storage(Agent).get("acme__asst")
    assert stored is not None
    assert stored.harness_id == harness.id


# ---------------------------------------------------------------------------
# 7. apply_install — writes HarnessRendering snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_install_writes_rendering_snapshot(fake_storage_provider):
    """apply_install writes a HarnessRendering row with id == harness.id."""
    harness = _make_harness("acme")

    agent_entry = RenderedEntry(
        kind="agent", template_name="asst",
        resolved_id="acme__asst",
        template_source_hash="h", rendered_hash="h1",
        rendered_payload={
            "description": "assistant",
            "model": {"provider_id": "p", "model_name": "m"},
        },
    )

    error = await apply_install(
        storage_provider=fake_storage_provider,
        harness=harness,
        entries=[agent_entry],
        rendered_files_by_name={},
        bundle_hash="bh1",
        overrides_hash="oh1",
        schema_hash="sh1",
    )

    assert error is None
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get(harness.id)
    assert rendering is not None
    assert rendering.id == harness.id
    assert rendering.harness_id == harness.id
    assert rendering.bundle_hash == "bh1"
    assert rendering.overrides_hash == "oh1"
    assert rendering.schema_hash == "sh1"
    assert len(rendering.entries) == 1
    assert rendering.entries[0].template_name == "asst"


# ---------------------------------------------------------------------------
# 8. apply_uninstall — reverse order + removes rendering + harness row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_uninstall_deletes_in_reverse_order(fake_storage_provider):
    """Uninstall deletes graph → agent → document → collection → toolset."""
    harness = _make_harness("acme")

    from primer.model.provider import Toolset, ToolsetProviderType
    from primer.model.collection import Collection, CollectionEmbedder, Document
    from primer.model.agent import Agent, AgentModel
    from primer.model.graph import Graph

    # Seed the storage with managed entities
    await fake_storage_provider.get_storage(Toolset).create(
        Toolset(id="acme__ts", provider=ToolsetProviderType.INTERNAL, harness_id=harness.id)
    )
    await fake_storage_provider.get_storage(Collection).create(
        Collection(
            id="acme__col", description="col",
            embedder=CollectionEmbedder(provider_id="ep", model="m"),
            search_provider_id="ssp",
            harness_id=harness.id,
        )
    )
    await fake_storage_provider.get_storage(Document).create(
        Document(id="acme__doc", collection_id="acme__col", name="doc", meta={}, harness_id=harness.id)
    )
    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id="acme__asst", description="asst",
            model=AgentModel(provider_id="p", model_name="m"),
            harness_id=harness.id,
        )
    )
    await fake_storage_provider.get_storage(Graph).create(
        Graph(
            id="acme__wf", description="wf",
            nodes=[{"kind": "terminal", "id": "t"}],
            edges=[],
            entry_node_id="t",
            harness_id=harness.id,
        )
    )

    # Write the rendering snapshot
    rendering = HarnessRendering(
        id=harness.id, harness_id=harness.id,
        bundle_hash="bh1", overrides_hash="oh1", schema_hash=None,
        entries=[
            RenderedEntry(kind="toolset", template_name="ts", resolved_id="acme__ts",
                          template_source_hash="h", rendered_hash="h1", rendered_payload={}),
            RenderedEntry(kind="collection", template_name="col", resolved_id="acme__col",
                          template_source_hash="h", rendered_hash="h2", rendered_payload={}),
            RenderedEntry(kind="document", template_name="doc", resolved_id="acme__doc",
                          template_source_hash="h", rendered_hash="h3", rendered_payload={}),
            RenderedEntry(kind="agent", template_name="asst", resolved_id="acme__asst",
                          template_source_hash="h", rendered_hash="h4", rendered_payload={}),
            RenderedEntry(kind="graph", template_name="wf", resolved_id="acme__wf",
                          template_source_hash="h", rendered_hash="h5", rendered_payload={}),
        ],
        rendered_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(HarnessRendering).create(rendering)
    await fake_storage_provider.get_storage(Harness).create(harness)

    deleted_kinds: list[str] = []

    orig_del_ts = fake_storage_provider.get_storage(Toolset).delete
    orig_del_col = fake_storage_provider.get_storage(Collection).delete
    orig_del_doc = fake_storage_provider.get_storage(Document).delete
    orig_del_agent = fake_storage_provider.get_storage(Agent).delete
    orig_del_graph = fake_storage_provider.get_storage(Graph).delete

    async def del_ts(id):
        deleted_kinds.append("toolset")
        return await orig_del_ts(id)

    async def del_col(id):
        deleted_kinds.append("collection")
        return await orig_del_col(id)

    async def del_doc(id):
        deleted_kinds.append("document")
        return await orig_del_doc(id)

    async def del_agent(id):
        deleted_kinds.append("agent")
        return await orig_del_agent(id)

    async def del_graph(id):
        deleted_kinds.append("graph")
        return await orig_del_graph(id)

    fake_storage_provider.get_storage(Toolset).delete = del_ts
    fake_storage_provider.get_storage(Collection).delete = del_col
    fake_storage_provider.get_storage(Document).delete = del_doc
    fake_storage_provider.get_storage(Agent).delete = del_agent
    fake_storage_provider.get_storage(Graph).delete = del_graph

    await apply_uninstall(storage_provider=fake_storage_provider, harness=harness)

    # Verify deletion order: graph → agent → document → collection → toolset
    assert deleted_kinds == ["graph", "agent", "document", "collection", "toolset"]

    # Rendering row removed
    r = await fake_storage_provider.get_storage(HarnessRendering).get(harness.id)
    assert r is None

    # Harness row removed
    h = await fake_storage_provider.get_storage(Harness).get(harness.id)
    assert h is None


# ---------------------------------------------------------------------------
# 9. apply_sync — fast path when hashes match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_sync_noop_when_hashes_match(fake_storage_provider):
    """apply_sync with matching hashes skips storage mutations (fast path)."""
    harness = _make_harness("acme")
    bundle_hash = "same-hash"
    overrides_hash = "same-oh"

    # Store an existing rendering snapshot
    existing_rendering = HarnessRendering(
        id=harness.id, harness_id=harness.id,
        bundle_hash=bundle_hash, overrides_hash=overrides_hash, schema_hash=None,
        entries=[
            RenderedEntry(
                kind="agent", template_name="asst", resolved_id="acme__asst",
                template_source_hash="h", rendered_hash="r1",
                rendered_payload={
                    "description": "assistant",
                    "model": {"provider_id": "p", "model_name": "m"},
                },
            ),
        ],
        rendered_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(HarnessRendering).create(existing_rendering)

    # Track any mutations to agent storage
    from primer.model.agent import Agent
    agent_mutations: list[str] = []
    original_create = fake_storage_provider.get_storage(Agent).create
    original_update = fake_storage_provider.get_storage(Agent).update
    original_delete = fake_storage_provider.get_storage(Agent).delete

    async def tracked_create(entity):
        agent_mutations.append("create")
        return await original_create(entity)

    async def tracked_update(entity):
        agent_mutations.append("update")
        return await original_update(entity)

    async def tracked_delete(id):
        agent_mutations.append("delete")
        return await original_delete(id)

    fake_storage_provider.get_storage(Agent).create = tracked_create
    fake_storage_provider.get_storage(Agent).update = tracked_update
    fake_storage_provider.get_storage(Agent).delete = tracked_delete

    # New entries with same rendered hashes
    new_entries = [
        RenderedEntry(
            kind="agent", template_name="asst", resolved_id="acme__asst",
            template_source_hash="h", rendered_hash="r1",
            rendered_payload={
                "description": "assistant",
                "model": {"provider_id": "p", "model_name": "m"},
            },
        ),
    ]

    error = await apply_sync(
        storage_provider=fake_storage_provider,
        harness=harness,
        new_entries=new_entries,
        rendered_files_by_name={},
        bundle_hash=bundle_hash,
        overrides_hash=overrides_hash,
        schema_hash=None,
    )

    assert error is None
    # No mutations should have occurred (fast path)
    assert agent_mutations == []


# ---------------------------------------------------------------------------
# 10. apply_sync — creates newly added entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_sync_creates_added_entries(fake_storage_provider):
    """apply_sync creates new entries (not in old rendering) with harness_id set."""
    harness = _make_harness("acme")

    # Existing rendering with no entries (empty bundle)
    old_rendering = HarnessRendering(
        id=harness.id, harness_id=harness.id,
        bundle_hash="old-bh", overrides_hash="oh1", schema_hash=None,
        entries=[],
        rendered_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(HarnessRendering).create(old_rendering)

    new_entries = [
        RenderedEntry(
            kind="agent", template_name="new-agent", resolved_id="acme__new-agent",
            template_source_hash="h", rendered_hash="r1",
            rendered_payload={
                "description": "new agent",
                "model": {"provider_id": "p", "model_name": "m"},
            },
        ),
    ]

    error = await apply_sync(
        storage_provider=fake_storage_provider,
        harness=harness,
        new_entries=new_entries,
        rendered_files_by_name={},
        bundle_hash="new-bh",
        overrides_hash="oh1",
        schema_hash=None,
    )

    assert error is None

    from primer.model.agent import Agent
    stored = await fake_storage_provider.get_storage(Agent).get("acme__new-agent")
    assert stored is not None
    assert stored.harness_id == harness.id


@pytest.mark.asyncio
async def test_apply_sync_deletes_removed_entries(fake_storage_provider):
    """apply_sync deletes entries that existed in old rendering but not in new."""
    harness = _make_harness("acme")

    from primer.model.agent import Agent, AgentModel

    # Create the agent in storage (from prior install)
    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id="acme__old-agent", description="old",
            model=AgentModel(provider_id="p", model_name="m"),
            harness_id=harness.id,
        )
    )

    old_rendering = HarnessRendering(
        id=harness.id, harness_id=harness.id,
        bundle_hash="old-bh", overrides_hash="oh1", schema_hash=None,
        entries=[
            RenderedEntry(
                kind="agent", template_name="old-agent", resolved_id="acme__old-agent",
                template_source_hash="h", rendered_hash="r1",
                rendered_payload={
                    "description": "old",
                    "model": {"provider_id": "p", "model_name": "m"},
                },
            ),
        ],
        rendered_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(HarnessRendering).create(old_rendering)

    # New bundle has no agents
    error = await apply_sync(
        storage_provider=fake_storage_provider,
        harness=harness,
        new_entries=[],
        rendered_files_by_name={},
        bundle_hash="new-bh",
        overrides_hash="oh1",
        schema_hash=None,
    )

    assert error is None
    deleted = await fake_storage_provider.get_storage(Agent).get("acme__old-agent")
    assert deleted is None


@pytest.mark.asyncio
async def test_apply_sync_updates_changed_entries(fake_storage_provider):
    """apply_sync updates entries whose rendered_hash differs."""
    harness = _make_harness("acme")

    from primer.model.agent import Agent, AgentModel

    # Create the agent with old payload
    await fake_storage_provider.get_storage(Agent).create(
        Agent(
            id="acme__asst", description="old description",
            model=AgentModel(provider_id="p", model_name="m"),
            harness_id=harness.id,
        )
    )

    old_rendering = HarnessRendering(
        id=harness.id, harness_id=harness.id,
        bundle_hash="old-bh", overrides_hash="oh1", schema_hash=None,
        entries=[
            RenderedEntry(
                kind="agent", template_name="asst", resolved_id="acme__asst",
                template_source_hash="h", rendered_hash="r-old",
                rendered_payload={
                    "description": "old description",
                    "model": {"provider_id": "p", "model_name": "m"},
                },
            ),
        ],
        rendered_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(HarnessRendering).create(old_rendering)

    new_entries = [
        RenderedEntry(
            kind="agent", template_name="asst", resolved_id="acme__asst",
            template_source_hash="h", rendered_hash="r-new",
            rendered_payload={
                "description": "new description",
                "model": {"provider_id": "p", "model_name": "m"},
            },
        ),
    ]

    error = await apply_sync(
        storage_provider=fake_storage_provider,
        harness=harness,
        new_entries=new_entries,
        rendered_files_by_name={},
        bundle_hash="new-bh",
        overrides_hash="oh1",
        schema_hash=None,
    )

    assert error is None
    updated = await fake_storage_provider.get_storage(Agent).get("acme__asst")
    assert updated is not None
    assert updated.description == "new description"


@pytest.mark.asyncio
async def test_apply_sync_replaces_rendering_snapshot(fake_storage_provider):
    """apply_sync replaces the stored rendering snapshot with the new one."""
    harness = _make_harness("acme")

    old_rendering = HarnessRendering(
        id=harness.id, harness_id=harness.id,
        bundle_hash="old-bh", overrides_hash="oh1", schema_hash=None,
        entries=[],
        rendered_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(HarnessRendering).create(old_rendering)

    new_entries = [
        RenderedEntry(
            kind="agent", template_name="asst", resolved_id="acme__asst",
            template_source_hash="h", rendered_hash="r1",
            rendered_payload={
                "description": "assistant",
                "model": {"provider_id": "p", "model_name": "m"},
            },
        ),
    ]

    error = await apply_sync(
        storage_provider=fake_storage_provider,
        harness=harness,
        new_entries=new_entries,
        rendered_files_by_name={},
        bundle_hash="new-bh",
        overrides_hash="new-oh",
        schema_hash="sh1",
    )

    assert error is None
    rendering = await fake_storage_provider.get_storage(HarnessRendering).get(harness.id)
    assert rendering.bundle_hash == "new-bh"
    assert rendering.overrides_hash == "new-oh"
    assert rendering.schema_hash == "sh1"
    assert len(rendering.entries) == 1


# ---------------------------------------------------------------------------
# Regression: harness_id in rendered payload MUST NOT override dispatch value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_install_payload_cannot_override_harness_id(fake_storage_provider):
    """If a template's rendered payload accidentally carries a harness_id,
    the dispatch's own value MUST win — otherwise the harness-managed lock
    is bypassable by smuggling a different harness_id through a template."""
    harness = _make_harness("acme")

    poisoned = RenderedEntry(
        kind="agent", template_name="asst",
        resolved_id="acme__asst",
        template_source_hash="h", rendered_hash="r",
        rendered_payload={
            "harness_id": "attacker-controlled-value",
            "description": "assistant",
            "model": {"provider_id": "p", "model_name": "m"},
        },
    )

    error = await apply_install(
        storage_provider=fake_storage_provider,
        harness=harness,
        entries=[poisoned],
        rendered_files_by_name={},
        bundle_hash="bh", overrides_hash="oh", schema_hash=None,
    )
    assert error is None

    from primer.model.agent import Agent
    stored = await fake_storage_provider.get_storage(Agent).get("acme__asst")
    assert stored.harness_id == harness.id, (
        "Dispatch's harness_id must win over template payload"
    )


# ---------------------------------------------------------------------------
# Regression: apply_sync per-entity failure should be reported via return
# (the dispatch is then responsible for NOT stamping bundle_hash). Here we
# pin the contract that apply_sync surfaces partial failures.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_sync_surfaces_partial_apply_errors(fake_storage_provider):
    """When a create inside apply_sync raises, apply_sync returns a
    partial_apply_failure error JSON (so dispatch can avoid stamping
    bundle_hash) — but the snapshot is still written with the new
    entries (so the next sync diffs against the right baseline)."""
    harness = _make_harness("acme")

    # Pre-seed an old rendering so we hit the non-fast-path branch.
    old_rendering = HarnessRendering(
        id=harness.id, harness_id=harness.id,
        bundle_hash="old-bh", overrides_hash="old-oh", schema_hash=None,
        entries=[], rendered_at=datetime.now(timezone.utc),
    )
    await fake_storage_provider.get_storage(HarnessRendering).create(old_rendering)

    entry = RenderedEntry(
        kind="agent", template_name="asst", resolved_id="acme__asst",
        template_source_hash="h", rendered_hash="r",
        rendered_payload={
            "description": "assistant",
            "model": {"provider_id": "p", "model_name": "m"},
        },
    )

    # Wrap the Agent storage to raise on create, simulating a per-entity
    # apply failure (e.g. unique-id conflict with a hand-created entity).
    from primer.model.agent import Agent
    agent_storage = fake_storage_provider.get_storage(Agent)
    orig_create = agent_storage.create

    async def boom(_obj):
        raise RuntimeError("simulated id conflict")

    agent_storage.create = boom  # type: ignore[assignment]
    try:
        error = await apply_sync(
            storage_provider=fake_storage_provider,
            harness=harness,
            new_entries=[entry],
            rendered_files_by_name={},
            bundle_hash="new-bh", overrides_hash="new-oh", schema_hash=None,
        )
    finally:
        agent_storage.create = orig_create  # type: ignore[assignment]
    assert error is not None
    import json as _json
    parsed = _json.loads(error)
    assert parsed["code"] == "partial_apply_failure"
