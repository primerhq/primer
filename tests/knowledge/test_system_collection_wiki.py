"""Task 11: regeneration writes the wiki behind the stamp, the live map under this-install/.

The platform wiki (the hand-written pages plus the generated reference
pages) is the versioned part: a build writes it only when the stamp lets
it, and a degraded build (no hand-written pages, or a skipped toolset)
never claims and never deletes. The live install map is written whatever
the decision.
"""
from __future__ import annotations

import logging

import pytest
import pytest_asyncio

from primer.knowledge.system_collection import (
    SYSTEM_COLLECTION_ID,
    WIKI_TITLE,
    converge_entity,
    regenerate_system_collection,
)
from primer.knowledge.tree import DocumentTreeService
from primer.knowledge.wiki.stamp import read_stamp
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from primer.toolset.misc import build_misc_toolset

MISC = {"misc": build_misc_toolset()}

_CALLING_TOOLS_LINE = (
    "Call any tool through system__call_tool; the door checks the inner role."
)

WIKI_FILES: dict[str, str] = {
    "index.md": (
        "---\n"
        "title: Platform wiki\n"
        "summary: Every platform feature an agent can use, and the exact tool calls for it.\n"
        "---\n"
        "\n"
        "The platform wiki. Read start-here first.\n"
    ),
    "start-here/index.md": (
        "---\n"
        "title: Start here\n"
        "summary: Read these first.\n"
        "---\n"
        "\n"
        "How to use this wiki.\n"
    ),
    "start-here/calling-tools.md": (
        "---\n"
        "title: Calling tools\n"
        "summary: How call_tool works.\n"
        "---\n"
        "\n"
        f"{_CALLING_TOOLS_LINE}\n"
        "See [get_datetime](reference/tools/misc/get_datetime) for the exact call.\n"
    ),
}


@pytest_asyncio.fixture
async def sp(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """A hand-written wiki plus an (empty) ai-docs dir, pinned by env vars."""
    root = tmp_path / "wiki"
    (root / "start-here").mkdir(parents=True)
    for name, text in WIKI_FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("PRIMER_WIKI_DIR", str(root))
    docs = tmp_path / "ai-docs"
    docs.mkdir()
    monkeypatch.setenv("PRIMER_AI_DOCS_DIR", str(docs))
    monkeypatch.setattr(
        "primer.knowledge.system_collection.RESERVED_TOOLSET_IDS",
        frozenset({"misc"}),
    )
    return root


async def _read(sp, path: str):
    return await DocumentTreeService(sp).read(
        collection_id=SYSTEM_COLLECTION_ID, path=path,
    )


def _set_body_line(root, rel: str, line: str) -> None:
    """Replace the first body line of a wiki source file with ``line``."""
    path = root / rel
    lines = path.read_text(encoding="utf-8").split("\n")
    closers = [i for i, l in enumerate(lines) if l.rstrip() == "---"]
    i = closers[1] + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    lines[i] = line
    path.write_text("\n".join(lines), encoding="utf-8")


async def test_a_pass_writes_the_wiki_with_titles_and_summaries(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    res = await DocumentTreeService(sp).read(collection_id="system", path="start-here/calling-tools")
    assert res.document.title == "Calling tools" and res.document.meta["summary"] == "How call_tool works."
    assert res.body.startswith("# Calling tools\n")
    assert (await DocumentTreeService(sp).read(collection_id="system", path="index")).document.title == "Platform wiki"


async def test_every_tool_gets_a_reference_page(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    for path in ("reference/tools/misc/get_datetime", "reference/tools/workspace/read", "reference/tools/client/open_file"):
        await DocumentTreeService(sp).read(collection_id="system", path=path)


async def test_the_live_map_lives_under_this_install(sp, wiki):
    await sp.get_storage(Agent).create(
        Agent(id="agent-a", description="helper",
              model=AgentModel(profile_id="prov--m"))
    )
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    page = await _read(sp, "this-install/agents/agent-a")
    assert "helper" in page.body
    with pytest.raises(NotFoundError):
        await _read(sp, "agents/agent-a")


async def test_an_old_layout_is_pruned_on_the_first_pass(sp, wiki):
    """The pre-slice-6 layout (root-level agents/, tools/, providers/) is
    deleted by the first build that may claim the stamp."""
    tree = DocumentTreeService(sp)
    # strict_slugs=False: the old layout was written by the regeneration,
    # which allows the tool ids' underscores.
    for parent, slug, body in (
        ("", "agents", "# Agents\n"),
        ("agents", "x", "# x\n\nold\n"),
        ("", "tools", "# Tools\n"),
        ("tools", "crud", "# crud\n"),
        ("tools/crud", "create_agent", "# create_agent\n"),
        ("", "providers", "# Providers\n"),
        ("providers", "llm", "# llm\n"),
    ):
        await tree.create(collection_id=SYSTEM_COLLECTION_ID, parent=parent,
                          slug=slug, body=body, strict_slugs=False)
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    for path in ("agents/x", "tools/crud/create_agent", "providers/llm"):
        with pytest.raises(NotFoundError):
            await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=path)


async def test_an_older_build_leaves_the_wiki_and_the_stamp_alone(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    _set_body_line(wiki, "start-here/calling-tools.md", "old text")
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    assert "old text" not in (await _read(sp, "start-here/calling-tools")).body
    assert (await read_stamp(sp)).app_version == "0.7.0"


async def test_a_superseded_build_leaves_the_wiki_alone(sp, wiki):
    """Build A claims; a same-version build B replaces it. Back to A: its
    hash is in ``superseded``, so a rolled-back A never writes again."""
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    a_hash = (await read_stamp(sp)).content_hash
    _set_body_line(wiki, "start-here/calling-tools.md", "build B text")
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    # Revert to build A's content and pass again.
    _set_body_line(wiki, "start-here/calling-tools.md", _CALLING_TOOLS_LINE)
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    assert "build B text" in (await _read(sp, "start-here/calling-tools")).body
    stamp = await read_stamp(sp)
    assert stamp.app_version == "0.6.0" and stamp.content_hash != a_hash
    assert a_hash in stamp.superseded


async def test_a_newer_build_rewrites_only_what_changed(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    _set_body_line(wiki, "start-here/calling-tools.md", "a single changed line")
    written = await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    assert written == 1, written


async def test_a_summary_change_alone_is_written(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    path = wiki / "index.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(
        "Every platform feature an agent can use, and the exact tool calls for it.",
        "Every platform feature, and the exact tool calls for it.",
    ), encoding="utf-8")
    written = await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    assert written == 1, written
    doc = (await _read(sp, "index")).document
    assert doc.meta["summary"] == "Every platform feature, and the exact tool calls for it."


async def test_the_live_map_is_converged_even_when_the_wiki_is_left_alone(sp, wiki):
    """The stamp gates the wiki only: an older build still writes the live map."""
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    await sp.get_storage(Agent).create(
        Agent(id="agent-late", description="arrived after the wiki settled",
              model=AgentModel(profile_id="prov--m"))
    )
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    page = await _read(sp, "this-install/agents/agent-late")
    assert "arrived after the wiki settled" in page.body
    assert (await read_stamp(sp)).app_version == "0.7.0"


async def test_a_malformed_page_keeps_the_stored_wiki_and_boots(sp, wiki, caplog):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    (wiki / "start-here" / "broken.md").write_text("no frontmatter\n")
    with caplog.at_level(logging.ERROR, logger="primer.knowledge.system_collection"):
        await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    # The stored wiki is intact and the boot did not raise.
    await _read(sp, "start-here/calling-tools")
    assert "broken.md" in caplog.text
    assert (await read_stamp(sp)).app_version == "0.6.0"


async def test_the_collection_is_titled_platform_wiki_and_keeps_its_search_block(sp, wiki):
    from primer.model.collection import CollectionEmbedder, CollectionSearchConfig

    colls = sp.get_storage(Collection)
    # A pre-wiki install: the old description and an operator-enabled search.
    await colls.create(Collection(
        id=SYSTEM_COLLECTION_ID, description="Primer system reference", system=True,
    ))
    coll = await colls.get(SYSTEM_COLLECTION_ID)
    await colls.update(coll.model_copy(update={
        "search": CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="emb", model="m"),
            vector_store_provider_id="ssp",
            state="ready",
        ),
    }))
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    coll = await colls.get(SYSTEM_COLLECTION_ID)
    assert coll.description == WIKI_TITLE
    assert coll.search is not None and coll.search.vector_store_provider_id == "ssp"


async def test_converge_entity_writes_under_this_install(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    await sp.get_storage(Agent).create(
        Agent(id="agent-cdc", description="written by the cdc hook",
              model=AgentModel(profile_id="prov--m"))
    )
    wrote = await converge_entity(sp, entity_type="agent", entity_id="agent-cdc")
    assert wrote is True
    page = await _read(sp, "this-install/agents/agent-cdc")
    assert "written by the cdc hook" in page.body
    index = await _read(sp, "this-install/agents")
    assert "this-install/agents/agent-cdc" in index.body
    await sp.get_storage(Agent).delete("agent-cdc")
    await converge_entity(sp, entity_type="agent", entity_id="agent-cdc")
    with pytest.raises(NotFoundError):
        await _read(sp, "this-install/agents/agent-cdc")


async def test_a_degraded_build_never_claims_and_keeps_the_stored_wiki(sp, wiki, monkeypatch):
    """Lead ruling (b): no hand-written pages, or a skipped toolset, and the
    build must not claim the stamp and must not delete stored pages."""
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    assert (await read_stamp(sp)).app_version == "0.6.0"

    # A higher build with no hand-written pages: claiming would push the
    # stored build's hash to superseded and stop it writing forever.
    monkeypatch.setenv("PRIMER_WIKI_DIR", str(wiki / "absent"))
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    assert (await read_stamp(sp)).app_version == "0.6.0"
    await _read(sp, "start-here/calling-tools")

    # A higher build with a skipped toolset: the same rule.
    monkeypatch.setenv("PRIMER_WIKI_DIR", str(wiki))

    class _Broken:
        async def list_tools(self, principal=None):
            raise RuntimeError("down")
            yield  # an async generator, like every toolset's list_tools

    await regenerate_system_collection(
        sp, toolset_providers={"misc": _Broken()}, app_version="0.7.0",
    )
    assert (await read_stamp(sp)).app_version == "0.6.0"
    await _read(sp, "reference/tools/misc/get_datetime")


async def test_orphaned_content_row_recovers_on_regenerate_and_converge(sp, wiki):
    from primer.model.collection import Document

    await sp.get_storage(Agent).create(
        Agent(id="agent-a", description="helper", model=AgentModel(profile_id="prov--m"))
    )
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    # Simulate an existing broken install where Document entity rows were deleted
    # without their content rows (both a wiki page and a live parent page).
    docs = sp.get_storage(Document)
    calling_doc = (await _read(sp, "start-here/calling-tools")).document
    agents_index_doc = (await _read(sp, "this-install/agents")).document
    await docs.delete(calling_doc.id)
    await docs.delete(agents_index_doc.id)

    _set_body_line(wiki, "index.md", "The platform wiki, updated.")
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")

    restored_calling = await _read(sp, "start-here/calling-tools")
    assert _CALLING_TOOLS_LINE in restored_calling.body
    restored_agents = await _read(sp, "this-install/agents")
    assert any(c.path == "this-install/agents/agent-a" for c in restored_agents.children)

    # Also verify converge_entity recovers if a live page or its parent index was orphaned.
    agent_doc = (await _read(sp, "this-install/agents/agent-a")).document
    await docs.delete(agent_doc.id)
    await sp.get_storage(Agent).update(
        Agent(id="agent-a", description="updated helper", model=AgentModel(profile_id="prov--m"))
    )
    assert await converge_entity(sp, entity_type="agent", entity_id="agent-a") is True
    assert "updated helper" in (await _read(sp, "this-install/agents/agent-a")).body


async def test_non_slug_live_entity_ids_get_collision_safe_slugs_and_do_not_stop_wiki(sp, wiki):
    from primer.model.graph import Graph

    await sp.get_storage(Graph).create(Graph(id="Team-Graph", description="upper"))
    await sp.get_storage(Graph).create(Graph(id="team-graph", description="lower"))
    await sp.get_storage(Collection).create(Collection(id="Docs", description="team docs"))

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    assert (await read_stamp(sp)).app_version == "0.6.0"
    await _read(sp, "start-here/calling-tools")
    lower = await _read(sp, "this-install/graphs/team-graph")
    assert "lower" in lower.body
    graphs_index = (await _read(sp, "this-install/graphs")).body
    assert "Team-Graph" in graphs_index and "team-graph" in graphs_index


async def test_a_bad_page_in_converge_or_live_pass_failure_does_not_stop_wiki_pass(sp, wiki, monkeypatch):
    import primer.knowledge.system_collection as sc

    real_write = sc._write_tree_page

    async def fail_one(tree, *, path, body, title, meta):
        if path == "this-install/workspaces":
            raise ValueError("simulated bad live page")
        await real_write(tree, path=path, body=body, title=title, meta=meta)

    monkeypatch.setattr(sc, "_write_tree_page", fail_one)
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    assert (await read_stamp(sp)).app_version == "0.6.0"
    await _read(sp, "start-here/calling-tools")

    # Even if _live_pages itself raises, _wiki_pass still runs and updates the wiki.
    async def boom_live(storage_provider):
        raise RuntimeError("live map crashed")

    monkeypatch.setattr(sc, "_live_pages", boom_live)
    _set_body_line(wiki, "start-here/calling-tools.md", "updated despite live crash")
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    assert (await read_stamp(sp)).app_version == "0.7.0"
    assert "updated despite live crash" in (await _read(sp, "start-here/calling-tools")).body


async def test_ruling_a_same_restores_missing_page_and_prunes_stale_wiki_pages(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    tree = DocumentTreeService(sp)
    await tree.delete(collection_id=SYSTEM_COLLECTION_ID, path="start-here/calling-tools")
    await tree.create(
        collection_id=SYSTEM_COLLECTION_ID, parent="", slug="agents",
        body="# Agents\n", strict_slugs=False,
    )
    await tree.create(
        collection_id=SYSTEM_COLLECTION_ID, parent="agents", slug="x",
        body="# x\n\nold\n", strict_slugs=False,
    )

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    await _read(sp, "start-here/calling-tools")
    with pytest.raises(NotFoundError):
        await _read(sp, "agents/x")


async def test_bounded_claim_loop_retries_and_stops_when_exhausted(sp, wiki, monkeypatch):
    import primer.knowledge.system_collection as sc

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    _set_body_line(wiki, "start-here/calling-tools.md", "0.7.0 text")

    attempts = 0
    real_claim = sc.claim

    async def fail_twice(storage_provider, *, app_version, content_hash):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return False
        return await real_claim(
            storage_provider, app_version=app_version, content_hash=content_hash,
        )

    monkeypatch.setattr(sc, "claim", fail_twice)
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.7.0")
    assert attempts == 3
    assert (await read_stamp(sp)).app_version == "0.7.0"

    # When all _CLAIM_PASSES attempts fail, the pass gives up cleanly without writing.
    _set_body_line(wiki, "start-here/calling-tools.md", "0.8.0 text")
    exhausted_attempts = 0

    async def always_lose(storage_provider, *, app_version, content_hash):
        nonlocal exhausted_attempts
        exhausted_attempts += 1
        return False

    monkeypatch.setattr(sc, "claim", always_lose)
    written = await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.8.0")
    assert exhausted_attempts == sc._CLAIM_PASSES
    assert (await read_stamp(sp)).app_version == "0.7.0"
    assert "0.8.0 text" not in (await _read(sp, "start-here/calling-tools")).body


async def test_missing_reserved_toolset_is_degraded_and_refuses_to_claim(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    await _read(sp, "reference/tools/misc/get_datetime")

    # Dropping the required built-in toolset from the map marks the pass degraded.
    await regenerate_system_collection(sp, toolset_providers={}, app_version="0.7.0")
    assert (await read_stamp(sp)).app_version == "0.6.0"
    await _read(sp, "reference/tools/misc/get_datetime")


async def test_converge_entity_preserves_live_index_summary(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    await sp.get_storage(Agent).create(
        Agent(id="agent-x", description="d", model=AgentModel(profile_id="p--m")),
    )
    await converge_entity(sp, entity_type="agent", entity_id="agent-x")
    after = (await _read(sp, "this-install/agents")).document.meta
    assert after["summary"] == "every agent on this install"


async def test_entity_created_mid_pass_is_not_pruned_by_prune_live(sp, wiki, monkeypatch):
    import primer.knowledge.system_collection as sc

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    real_live_pages = sc._live_pages

    async def snapshot_then_cdc(storage_provider):
        live = await real_live_pages(storage_provider)
        await sp.get_storage(Agent).create(
            Agent(id="agent-late", description="created mid-pass", model=AgentModel(profile_id="p--m")),
        )
        assert await converge_entity(sp, entity_type="agent", entity_id="agent-late") is True
        return live

    monkeypatch.setattr(sc, "_live_pages", snapshot_then_cdc)
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    page = await _read(sp, "this-install/agents/agent-late")
    assert "created mid-pass" in page.body
    index = (await _read(sp, "this-install/agents")).body
    assert "agent-late" in index


async def test_non_ascii_descriptions_and_workspace_names_remain_readable_utf8(sp, wiki):
    from datetime import UTC, datetime
    from pydantic import SecretStr
    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta

    await sp.get_storage(Agent).create(
        Agent(id="agent-utf8", description="café مرحبا", model=AgentModel(profile_id="p--m")),
    )
    await sp.get_storage(Workspace).create(
        Workspace(
            id="ws-utf8",
            name="café مرحبا",
            template_id="default",
            provider_id="local",
            created_at=datetime.now(UTC),
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("tok")),
        ),
    )
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    agent_page = (await _read(sp, "this-install/agents/agent-utf8")).body
    assert 'User-supplied description: "café مرحبا"' in agent_page
    assert "\\u" not in agent_page

    ws_page = (await _read(sp, "this-install/workspaces/ws-utf8")).body
    assert 'name (user-supplied): "café مرحبا"' in ws_page
    assert "\\u" not in ws_page

    ws_index = (await _read(sp, "this-install/workspaces")).body
    assert 'name (user-supplied): "café مرحبا"' in ws_index
    assert "\\u" not in ws_index


async def test_prune_live_does_not_keep_stale_slug_matching_hashed_entity_id(sp, wiki):
    from primer.knowledge.system_collection import _live_slug

    # Entity id "foo--deadbeef" maps to "foo--deadbeef--<hash>" because it ends
    # with a "--[0-9a-f]{8}" suffix. A stale page at "this-install/agents/foo--deadbeef"
    # must be pruned even though get("foo--deadbeef") returns the entity.
    entity_id = "foo--deadbeef"
    mapped_slug = _live_slug(entity_id)
    assert mapped_slug != entity_id

    await sp.get_storage(Agent).create(
        Agent(id=entity_id, description="hashed slug", model=AgentModel(profile_id="p--m")),
    )
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    tree = DocumentTreeService(sp)
    await tree.create(
        collection_id=SYSTEM_COLLECTION_ID,
        parent="this-install/agents",
        slug=entity_id,
        body="# stale\n",
        strict_slugs=False,
    )

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    await _read(sp, f"this-install/agents/{mapped_slug}")
    with pytest.raises(NotFoundError):
        await _read(sp, f"this-install/agents/{entity_id}")
