"""Task 12: wiki_read and wiki_search, the agent's eyes and ears on the platform wiki.

The system collection is seeded with the Task 11 fixture tree (the hand-written
pages, the generated reference layer, the live map), then the system toolset is
built over a registry stand-in whose ``get_toolset("collections")`` answers with
the real collections toolset, so ``wiki_search`` runs the real collections
``search`` (fulltext over the SQLite FTS mirror; the semantic rung answers its
own not-enabled error).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from primer.knowledge.system_collection import regenerate_system_collection
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from primer.toolset.collections import build_collections_toolset
from primer.toolset.system import build_system_toolset
from tests._support.caller import caller
from tests.knowledge.test_system_collection_wiki import (
    MISC,
    _CALLING_TOOLS_LINE,
    WIKI_FILES,
)

USER = caller("user")

# The Task 11 fixture tree, with one distinctive fulltext word added to the
# calling-tools page so a search can find it by a word it alone holds.
FILES: dict[str, str] = {
    **WIKI_FILES,
    "start-here/calling-tools.md": WIKI_FILES["start-here/calling-tools.md"].replace(
        f"{_CALLING_TOOLS_LINE}\n",
        f"{_CALLING_TOOLS_LINE} A zebrafish keeps the tank running.\n",
    ),
}


class _Registry:
    """The ProviderRegistry the system toolset sees: the collections toolset
    plus the no-op invalidation hooks the builder threads through its cascades."""

    def __init__(self, collections: Any) -> None:
        self._collections = collections

    async def get_toolset(self, toolset_id: str) -> Any:
        if toolset_id == "collections":
            return self._collections
        raise NotFoundError(f"toolset {toolset_id!r} not found")

    async def invalidate_llm(self, eid: str) -> None:
        pass

    async def invalidate_embedder(self, eid: str) -> None:
        pass

    async def invalidate_cross_encoder(self, eid: str) -> None:
        pass

    async def invalidate_toolset(self, eid: str) -> None:
        pass

    async def invalidate_aggregated_llm(self, eid: str) -> None:
        pass


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
    """The hand-written wiki (plus an empty ai-docs dir), pinned by env vars."""
    root = tmp_path / "wiki"
    (root / "start-here").mkdir(parents=True)
    for name, text in FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("PRIMER_WIKI_DIR", str(root))
    docs = tmp_path / "ai-docs"
    docs.mkdir()
    monkeypatch.setenv("PRIMER_AI_DOCS_DIR", str(docs))
    return root


@pytest_asyncio.fixture
async def system_toolset(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    collections = build_collections_toolset(storage_provider=sp)
    toolset = build_system_toolset(
        storage_provider=sp,
        provider_registry=_Registry(collections),
    )
    yield toolset


async def test_an_empty_path_reads_the_table_of_contents(system_toolset):
    r = await system_toolset.call(tool_name="wiki_read", ctx=USER, arguments={})
    out = json.loads(r.output)
    assert (out["path"], out["title"], out["kind"]) == ("index", "Platform wiki", "toc")
    assert set(out.keys()) == {"path", "title", "summary", "kind", "body", "children"}
    assert {"path": "start-here", "title": "Start here", "summary": "Read these first."} in out["children"]
    assert all(set(c.keys()) == {"path", "title", "summary"} for c in out["children"])
    # The Contents entries: the section, the reference root and the live map
    # root, plus the legacy roots (the shipped docs index and the in-code
    # how-to guides), the TOC itself aside.
    assert {c["path"] for c in out["children"]} == {
        "start-here", "reference", "this-install", "docs", "how-to",
    }


async def test_a_guide_reads_with_its_title_and_summary(system_toolset):
    r = await system_toolset.call(
        tool_name="wiki_read", ctx=USER, arguments={"path": "/start-here/calling-tools.md"},
    )
    out = json.loads(r.output)
    assert (out["path"], out["title"], out["summary"]) == (
        "start-here/calling-tools", "Calling tools", "How call_tool works.",
    )


async def test_a_missing_page_names_its_siblings(system_toolset):
    r = await system_toolset.call(
        tool_name="wiki_read", ctx=USER, arguments={"path": "start-here/nope"},
    )
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "not-found"
    assert "calling-tools" in body["message"]


async def test_wiki_search_finds_a_page_by_a_word_it_holds(system_toolset):
    r = await system_toolset.call(
        tool_name="wiki_search", ctx=USER,
        arguments={"query": "zebrafish", "mode": "fulltext"},
    )
    out = json.loads(r.output)
    assert out["mode_used"] == "fulltext"
    assert any(h["path"] == "start-here/calling-tools" for h in out["hits"])


async def test_wiki_search_honours_path_prefix(system_toolset):
    r = await system_toolset.call(
        tool_name="wiki_search", ctx=USER,
        arguments={"query": "zebrafish", "mode": "fulltext", "path_prefix": "reference/"},
    )
    # The word lives only under start-here/: the prefix hides it.
    assert json.loads(r.output)["hits"] == []
    r = await system_toolset.call(
        tool_name="wiki_search", ctx=USER,
        arguments={"query": "zebrafish", "mode": "fulltext", "path_prefix": "start-here/"},
    )
    out = json.loads(r.output)
    assert [h["path"] for h in out["hits"]] == ["start-here/calling-tools"]


async def test_semantic_without_an_index_passes_the_collections_error_through(
    system_toolset,
):
    r = await system_toolset.call(
        tool_name="wiki_search", ctx=USER,
        arguments={"query": "zebrafish", "mode": "semantic"},
    )
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "conflict"
    assert "semantic search is not enabled on 'system'" in body["message"]
