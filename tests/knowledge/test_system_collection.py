"""Unconditional system-collection regeneration (amendment M12, decision D-B)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from primer.knowledge.system_collection import (
    SYSTEM_COLLECTION_ID, regenerate_system_collection,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


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


async def test_regeneration_builds_subtrees_without_any_embedder(sp):
    await sp.get_storage(Agent).create(
        Agent(id="agent-a", description="helper",
              model=AgentModel(profile_id="prov--m"))
    )
    n = await regenerate_system_collection(sp, toolset_providers={})
    assert n > 0
    coll = await sp.get_storage(Collection).get(SYSTEM_COLLECTION_ID)
    assert coll is not None and coll.system is True and coll.search is None
    tree = DocumentTreeService(sp)
    res = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="agents/agent-a")
    assert "helper" in res.body
    idx = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="agents")
    assert "agent-a" in idx.body


async def test_regeneration_is_idempotent_and_prunes_stale(sp):
    agents = sp.get_storage(Agent)
    await agents.create(Agent(id="agent-a", description="helper",
                              model=AgentModel(profile_id="prov--m")))
    await regenerate_system_collection(sp, toolset_providers={})
    await agents.delete("agent-a")
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="agents/agent-a")


class _RecordingStore:
    """Vector store that only remembers what was written to it."""

    def __init__(self) -> None:
        self.records: dict = {}
        self.collections: dict = {}

    async def create_collection(self, cid, *, dimensions, distance="cosine"):
        self.collections[cid] = dimensions

    async def put(self, record):
        key = (record.collection_id, record.document_id, record.chunk_id)
        self.records[key] = record

    async def delete(self, cid, doc_id):
        for key in list(self.records):
            if key[0] == cid and key[1] == doc_id:
                del self.records[key]

    def documents(self) -> set[str]:
        return {doc_id for _, doc_id, _ in self.records}


class _Embedder:
    async def embed(self, *, model, inputs, **kwargs):
        class _R:
            embeddings = [
                type("E", (), {"vector": [0.1, 0.2, 0.3, 0.4]})() for _ in inputs
            ]

        return _R()


class _Registry:
    async def get_embedder(self, provider_id):
        return _Embedder()


class _SSR:
    def __init__(self, store: _RecordingStore) -> None:
        self._store = store

    async def get_store(self, ssp_id):
        return self._store


async def _enable_search(sp) -> None:
    from primer.model.collection import CollectionEmbedder, CollectionSearchConfig

    colls = sp.get_storage(Collection)
    coll = await colls.get(SYSTEM_COLLECTION_ID)
    await colls.update(coll.model_copy(update={
        "search": CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="emb", model="m"),
            vector_store_provider_id="ssp",
            state="ready",
        ),
    }))


async def test_regeneration_indexes_only_what_changed(sp):
    """A document the pass leaves alone must not be re-embedded.

    Regeneration used to rewrite every path on every run. That was
    invisible while nothing indexed; with the write-through hooks
    attached it would re-embed the whole map at each startup, so the
    pass has to converge rather than overwrite.
    """
    await sp.get_storage(Agent).create(
        Agent(id="agent-a", description="helper",
              model=AgentModel(profile_id="prov--m"))
    )
    await regenerate_system_collection(sp, toolset_providers={})
    await _enable_search(sp)

    store = _RecordingStore()
    kwargs = {
        "toolset_providers": {},
        "provider_registry": _Registry(),
        "semantic_search_registry": _SSR(store),
    }
    # Nothing about the platform changed, so nothing should be written.
    assert await regenerate_system_collection(sp, **kwargs) == 0
    assert not store.records

    # Change one agent: its page moves and nothing else does. The index
    # lists ids, not descriptions, so it is untouched here.
    agents = sp.get_storage(Agent)
    existing = await agents.get("agent-a")
    await agents.update(existing.model_copy(update={"description": "rewritten"}))
    written = await regenerate_system_collection(sp, **kwargs)
    assert written == 1, written
    assert len(store.documents()) == 1, store.documents()


async def test_entity_write_converges_and_indexes_that_entity(sp):
    """A new agent is searchable without waiting for the next startup.

    This is what the CDC hooks call on every agent, graph and collection
    mutation. Without it an agent created at runtime had no page in the
    system collection at all until the process restarted.
    """
    from primer.knowledge.system_collection import converge_entity

    await regenerate_system_collection(sp, toolset_providers={})
    await _enable_search(sp)

    store = _RecordingStore()
    await sp.get_storage(Agent).create(
        Agent(id="agent-new", description="a distinctive purpose",
              model=AgentModel(profile_id="prov--m"))
    )
    wrote = await converge_entity(
        sp, entity_type="agent", entity_id="agent-new",
        provider_registry=_Registry(), semantic_search_registry=_SSR(store),
    )
    assert wrote is True

    tree = DocumentTreeService(sp)
    page = await tree.read(collection_id=SYSTEM_COLLECTION_ID,
                           path="agents/agent-new")
    assert "a distinctive purpose" in page.body
    index = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="agents")
    assert "agents/agent-new" in index.body
    assert page.document.id in store.documents(), (
        "the new agent's page never reached the vector store"
    )

    # Deleting it takes the page away again.
    await sp.get_storage(Agent).delete("agent-new")
    await converge_entity(
        sp, entity_type="agent", entity_id="agent-new",
        provider_registry=_Registry(), semantic_search_registry=_SSR(store),
    )
    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID,
                        path="agents/agent-new")


async def test_the_index_hook_is_deferred_off_the_write() -> None:
    """The page write stays synchronous; embedding it does not.

    A CRUD create is expected to leave the entity's page readable the
    moment it returns, and a test pins exactly that. Embedding the page
    is a different matter: it calls the embedding provider, which on a
    cold container also downloads the model, so awaiting it inline put a
    network round trip inside every agent, graph and collection write.
    Five concurrent creates racing a bootstrap then queued behind each
    other until the client gave up.
    """
    import asyncio

    from primer.knowledge.system_collection import _deferred

    started = asyncio.Event()
    release = asyncio.Event()
    finished = []

    async def slow_indexer(*, document, content):
        started.set()
        await release.wait()
        finished.append(document)

    schedule = _deferred(slow_indexer)

    # The caller returns without waiting for the indexer to finish.
    await asyncio.wait_for(
        schedule(document="doc", content="body"), timeout=1.0,
    )
    assert finished == [], "the write waited for the embed"

    # ...and the indexer still runs.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    release.set()
    for _ in range(50):
        if finished:
            break
        await asyncio.sleep(0.01)
    assert finished == ["doc"], "the deferred index never ran"


async def test_a_failing_deferred_index_does_not_escape() -> None:
    """Searchability is best-effort; a write must not fail behind it."""
    import asyncio

    from primer.knowledge.system_collection import _deferred

    async def broken(*, document, content):
        raise RuntimeError("embedder down")

    await _deferred(broken)(document="doc", content="body")
    # Let the task run and be collected; nothing may propagate.
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_regeneration_survives_a_page_created_underneath_it(sp, monkeypatch):
    """A CDC convergence hook can win the create; the pass must not die.

    Seeding an agent fires converge_entity for ``agents/<id>``, the very
    path the regeneration that runs next is about to write. When that hook
    lands between the regeneration's not-found miss and its create, the
    create raises ConflictError. Before the fix that killed the whole
    ensure pass and left the system collection half built.
    """
    from primer.model.except_ import ConflictError

    await sp.get_storage(Agent).create(
        Agent(id="agent-raced", description="helper",
              model=AgentModel(profile_id="prov--m"))
    )

    real_create = DocumentTreeService.create
    raced: dict[str, bool] = {}

    async def racing_create(self, *, collection_id, parent, slug, body,
                            title=None, strict_slugs=True):
        # Stand in for the hook: write the page, then report the collision
        # the regeneration would really have seen.
        if slug == "agent-raced" and not raced.get("done"):
            raced["done"] = True
            await real_create(
                self, collection_id=collection_id, parent=parent, slug=slug,
                body="written by the convergence hook", title=title,
                strict_slugs=strict_slugs,
            )
            raise ConflictError(f"path {parent}/{slug!r} already exists")
        return await real_create(
            self, collection_id=collection_id, parent=parent, slug=slug,
            body=body, title=title, strict_slugs=strict_slugs,
        )

    monkeypatch.setattr(DocumentTreeService, "create", racing_create)

    await regenerate_system_collection(sp, toolset_providers={})

    assert raced.get("done"), "the racing create never fired"
    # The pass took the page over instead of leaving the hook's stand-in body.
    tree = DocumentTreeService(sp)
    page = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID, path="agents/agent-raced",
    )
    assert page.body != "written by the convergence hook"
    assert "agent-raced" in page.body
