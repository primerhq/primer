"""An upgrade converges the one wiki source and prunes the retired trees safely."""

from __future__ import annotations

import shutil
from unittest.mock import AsyncMock

import pytest

from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, regenerate_system_collection
from primer.knowledge.tree import DocumentTreeService
from primer.knowledge.wiki.stamp import claim, decide, read_stamp
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig, Document
from primer.model.except_ import NotFoundError
from primer.model.vector import EmbeddingRecord
from primer.model.wiki import WikiStamp
from tests.knowledge.test_indexing import _StatefulStore
from tests.knowledge.test_system_collection_wiki import MISC, sp, wiki  # noqa: F401


async def _old_pages(tree):
    documents = []
    for root in ("docs", "how-to"):
        documents.append(await tree.create(collection_id=SYSTEM_COLLECTION_ID, parent="", slug=root, body=f"# {root}\n"))
        documents.append(await tree.create(collection_id=SYSTEM_COLLECTION_ID, parent=root, slug="old", body="Retired source\n"))
    return documents


@pytest.mark.parametrize("decision", ["write", "same"])
async def test_upgrade_and_interrupted_same_build_remove_old_roots_and_repair_current_pages(sp, wiki, decision, tmp_path, monkeypatch):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    tree = DocumentTreeService(sp)
    retired = await _old_pages(tree)
    # An obsolete deployment override must not bring a second source back.
    obsolete = tmp_path / "old-agent-docs"
    obsolete.mkdir()
    (obsolete / "reintroduced.md").write_text("This must not be loaded\n")
    monkeypatch.setenv("PRIMER_AI_DOCS_DIR", str(obsolete))
    await tree.delete(collection_id=SYSTEM_COLLECTION_ID, path="start-here/calling-tools")
    kept = (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="index")).document
    collections = sp.get_storage(Collection)
    collection = await collections.get(SYSTEM_COLLECTION_ID)
    search = CollectionSearchConfig(embedder=CollectionEmbedder(provider_id="emb", model="m"), vector_store_provider_id="ssp")
    await collections.update(collection.model_copy(update={"search": search}))
    store = _StatefulStore()
    for document in [*retired, kept]:
        await store.put(EmbeddingRecord(collection_id=SYSTEM_COLLECTION_ID, document_id=document.id, chunk_id="0", text="indexed", vector=[1.0]))
        assert await sp.get_content_store().get(document.id) is not None
    registry = AsyncMock()
    registry.get_store.return_value = store
    prior = await read_stamp(sp)
    if decision == "write":
        prior = prior.model_copy(update={"content_hash": "pre-retirement-content", "source_hash": "pre-retirement-source"})
        await sp.get_storage(WikiStamp).update(prior)

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0", semantic_search_registry=registry)
    current = await read_stamp(sp)
    page = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="start-here/calling-tools")
    assert "get_datetime" in page.body
    index = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="index")
    assert "(docs)" not in index.body and "(how-to)" not in index.body
    for path in ("docs", "docs/old", "how-to", "how-to/old"):
        with pytest.raises(NotFoundError):
            await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=path)
        assert await sp.get_content_store().resolve_id(SYSTEM_COLLECTION_ID, path) is None
    for document in retired:
        assert await sp.get_storage(Document).get(document.id) is None
        assert await sp.get_content_store().get(document.id) is None
        assert await store.get(SYSTEM_COLLECTION_ID, document.id) == []
    assert await store.get(SYSTEM_COLLECTION_ID, kept.id)
    assert (await collections.get(SYSTEM_COLLECTION_ID)).search == search
    assert (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="this-install")).body
    if decision == "write":
        assert prior.content_hash in current.superseded
        assert decide(current, app_version="0.6.0", content_hash=prior.content_hash, source_hash=prior.source_hash) == "superseded"
        assert not await claim(sp, app_version="0.6.0", content_hash=prior.content_hash, source_hash=prior.source_hash)
    else:
        assert current == prior
    assert await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0") == 0


async def test_lost_retirement_claim_preserves_legacy_content_and_does_not_repair_as_owner(sp, wiki, monkeypatch):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    tree = DocumentTreeService(sp)
    retired = await _old_pages(tree)
    await tree.delete(collection_id=SYSTEM_COLLECTION_ID, path="start-here/calling-tools")
    prior = (await read_stamp(sp)).model_copy(update={"content_hash": "old", "source_hash": "old-source"})
    await sp.get_storage(WikiStamp).update(prior)
    refused = AsyncMock(return_value=False)
    monkeypatch.setattr("primer.knowledge.system_collection.claim", refused)

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    assert refused.await_count == 3
    assert await read_stamp(sp) == prior
    for document in retired:
        assert await sp.get_storage(Document).get(document.id) is not None
        assert await sp.get_content_store().get(document.id) is not None
    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="start-here/calling-tools")


async def test_a_peer_taking_the_stamp_during_retirement_stops_further_deletes(sp, wiki, monkeypatch):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    tree = DocumentTreeService(sp)
    retired = await _old_pages(tree)
    previous = await read_stamp(sp)
    original_delete = DocumentTreeService.delete
    deleted = []

    async def delete_then_peer_claim(self, **kwargs):
        result = await original_delete(self, **kwargs)
        if kwargs["path"].startswith(("docs/", "how-to/")):
            deleted.append(kwargs["path"])
            assert await claim(sp, app_version="0.6.0", content_hash="peer-content", source_hash="peer-source")
        return result

    monkeypatch.setattr(DocumentTreeService, "delete", delete_then_peer_claim)
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    assert len(deleted) == 1
    assert (await read_stamp(sp)).content_hash == "peer-content"
    remaining = [document for document in retired if await sp.get_content_store().get(document.id) is not None]
    assert len(remaining) == 3
    assert previous.content_hash in (await read_stamp(sp)).superseded
    assert (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="start-here/calling-tools")).body


async def test_upgraded_installed_layout_ignores_leftover_agent_docs_and_override(sp, wiki, monkeypatch, tmp_path):
    import primer

    package = tmp_path / "site-packages/primer"
    current = package / "_docs/wiki"
    shutil.copytree(wiki, current)
    obsolete = package / "_docs/agents"
    obsolete.mkdir()
    (obsolete / "stale.md").write_text("Retired deployment override must never reappear")
    monkeypatch.setattr(primer, "__file__", str(package / "__init__.py"))
    monkeypatch.delenv("PRIMER_WIKI_DIR")
    monkeypatch.setenv("PRIMER_AI_DOCS_DIR", str(obsolete))

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    tree = DocumentTreeService(sp)
    assert (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="start-here/calling-tools")).body
    roots = {node.slug for node in await tree.tree(collection_id=SYSTEM_COLLECTION_ID, depth=1)}
    assert {"start-here", "reference", "this-install"} <= roots
    assert not {"docs", "how-to"} & roots
    assert await sp.get_content_store().search_fulltext(SYSTEM_COLLECTION_ID, "Retired deployment override") == []


async def test_degraded_retirement_input_does_not_claim_or_prune_the_stored_legacy_pages(sp, wiki, monkeypatch, tmp_path):
    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")
    tree = DocumentTreeService(sp)
    await _old_pages(tree)
    before = await read_stamp(sp)
    empty = tmp_path / "missing-wiki"
    monkeypatch.setenv("PRIMER_WIKI_DIR", str(empty))

    await regenerate_system_collection(sp, toolset_providers=MISC, app_version="0.6.0")

    assert await read_stamp(sp) == before
    for path in ("docs/old", "how-to/old", "start-here/calling-tools"):
        assert (await tree.read(collection_id=SYSTEM_COLLECTION_ID, path=path)).body
