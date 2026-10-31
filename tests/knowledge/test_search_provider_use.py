"""A collection's search providers are USED by the owner who enables search and by everyone who searches (access-control P4 Task 6).

Enabling needs the embedder, the vector store and (when set) the cross-encoder to be usable by the owner, else the existing not-registered 409.
A search by a person who may not use them is refused (REST 409, the tool falls back to fulltext on ``auto`` and refuses ``semantic``); grep and the
tree stay available. Indexing writes are not a use of their own (lead answer 4).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from primer.access.person import Person
from primer.knowledge.lifecycle import enable_search, validate_search_config
from primer.model.access import Access
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig
from primer.model.except_ import ConflictError
from primer.model.principal import PrincipalRef
from primer.model.provider import (
    EmbeddingModel, EmbeddingProvider, EmbeddingProviderType, HuggingFaceConfig, Limits, SemanticSearchProvider, SqliteConfig,
)
from primer.model.user import User
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.collections import build_collections_toolset
from tests.knowledge.test_indexing import _Emb, _Store

ADA, ROOT = Person("ada", "user"), Person("root", "admin")
GRANT_ADA = Access(grants={"ada": "read"}, rev=1)


def _embedder(pid: str, access: Access) -> EmbeddingProvider:
    return EmbeddingProvider(
        id=pid, provider=EmbeddingProviderType.HUGGINGFACE, models=[EmbeddingModel(name="m")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")), limits=Limits(max_concurrency=1), access=access,
    )


def _ssp(pid: str, access: Access) -> SemanticSearchProvider:
    return SemanticSearchProvider.model_validate({
        "id": pid, "provider": "pgvector", "access": access.model_dump(mode="json"),
        "config": {"hostname": "localhost", "port": 5432, "database": "primer", "username": "primer", "password": "primer", "db_schema": "public"},
    })


def _cfg(emb: str, ssp: str, state: str = "indexing") -> CollectionSearchConfig:
    return CollectionSearchConfig(embedder=CollectionEmbedder(provider_id=emb, model="m"), vector_store_provider_id=ssp, state=state)


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    for uid in ("ada", "root"):
        await provider.get_storage(User).create(User(id=uid, username=uid, created_at=datetime.now(UTC), role="admin" if uid == "root" else "user"))
    for emb, ssp, access in (("emb-pub", "ssp-pub", Access(public=True)), ("emb-priv", "ssp-priv", Access()), ("emb-ada", "ssp-ada", GRANT_ADA)):
        await provider.get_storage(EmbeddingProvider).create(_embedder(emb, access))
        await provider.get_storage(SemanticSearchProvider).create(_ssp(ssp, access))
    await provider.get_storage(Collection).create(Collection(id="kb", description="wiki", access=Access(owners=["ada"])))
    yield provider
    await provider.aclose()


async def test_enable_needs_provider_use(sp):
    await validate_search_config(sp, _cfg("emb-pub", "ssp-pub"), ADA)
    await validate_search_config(sp, _cfg("emb-ada", "ssp-ada"), ADA)
    await validate_search_config(sp, _cfg("emb-priv", "ssp-priv"), ROOT)  # an admin uses any
    await validate_search_config(sp, _cfg("emb-priv", "ssp-priv"), None)  # internal callers are unchecked

    with pytest.raises(ConflictError, match="is not registered"):
        await validate_search_config(sp, _cfg("emb-priv", "ssp-pub"), ADA)  # the embedder
    with pytest.raises(ConflictError, match="is not registered"):
        await validate_search_config(sp, _cfg("emb-pub", "ssp-priv"), ADA)  # the vector store


async def test_enable_search_refuses_and_writes_nothing_for_an_unusable_provider(sp):
    reg, ssr = AsyncMock(), AsyncMock()

    with pytest.raises(ConflictError):
        await enable_search(sp, reg, ssr, collection_id="kb", cfg=_cfg("emb-priv", "ssp-pub"), person=ADA)

    assert (await sp.get_storage(Collection).get("kb")).search is None
    reg.get_embedder.assert_not_called()


async def test_enable_search_works_with_usable_providers(sp):
    reg, ssr = AsyncMock(), AsyncMock()
    reg.get_embedder = AsyncMock(return_value=_Emb(dim=4))
    ssr.get_store = AsyncMock(return_value=_Store())

    updated = await enable_search(sp, reg, ssr, collection_id="kb", cfg=_cfg("emb-ada", "ssp-ada"), person=ADA)

    assert updated.search is not None and updated.search.embedder.provider_id == "emb-ada"


def _ctx(uid: str, role: str = "user") -> ToolContext:
    ref = PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
    return ToolContext(tool_call_id="c", session_id="s", workspace_id="w", initiated_by=ref)


async def _toolset(sp, search: CollectionSearchConfig):
    store = sp.get_storage(Collection)
    row = await store.get("kb")
    await store.update(row.model_copy(update={"access": Access(public=True), "search": search}))
    return build_collections_toolset(storage_provider=sp, provider_registry=AsyncMock(), semantic_search_registry=AsyncMock())


async def test_search_needs_provider_use(sp):
    tools = await _toolset(sp, _cfg("emb-priv", "ssp-pub", "ready"))

    for uid, expected in (("ada", "fulltext"), ("root", "semantic")):
        listed = await tools.call(tool_name="collections_list", arguments={}, ctx=_ctx(uid, "admin" if uid == "root" else "user"))
        assert json.loads(listed.output)["collections"][0]["search"] == expected, uid

    refused = await tools.call(tool_name="search", arguments={"collection": "kb", "query": "x", "mode": "semantic"}, ctx=_ctx("ada"))
    assert refused.is_error and "not available to you" in refused.output and json.loads(refused.output)["type"] == "conflict"


async def test_auto_falls_back_to_fulltext(sp):
    tools = await _toolset(sp, _cfg("emb-priv", "ssp-pub", "ready"))

    result = await tools.call(tool_name="search", arguments={"collection": "kb", "query": "x"}, ctx=_ctx("ada"))

    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert body["mode_used"] == "fulltext" and "not available to you" in body["note"]


async def test_a_person_with_the_grants_is_offered_semantic(sp):
    tools = await _toolset(sp, _cfg("emb-ada", "ssp-ada", "ready"))

    listed = await tools.call(tool_name="collections_list", arguments={}, ctx=_ctx("ada"))

    assert json.loads(listed.output)["collections"][0]["search"] == "semantic"


class _CountingEmb(_Emb):
    def __init__(self) -> None:
        super().__init__(dim=4)
        self.calls = 0

    async def embed(self, *, model, inputs):
        self.calls += 1
        return await super().embed(model=model, inputs=inputs)


async def _owned_kb(sp, search: CollectionSearchConfig) -> Collection:
    store = sp.get_storage(Collection)
    return await store.update((await store.get("kb")).model_copy(update={"search": search}))


async def test_indexing_refuses_a_provider_no_owner_may_use(sp):
    """Security review finding 2, defence in depth: indexing never embeds through a provider no owner of the collection may use."""
    from primer.knowledge.indexing import index_document
    from primer.model.collection import Document
    from primer.model.except_ import PrimerError
    from tests.knowledge.test_indexing import _ContentStore

    emb, reg, ssr = _CountingEmb(), AsyncMock(), AsyncMock()
    reg.get_embedder = AsyncMock(return_value=emb)
    ssr.get_store = AsyncMock(return_value=_Store())
    doc = Document(id="d1", collection_id="kb", slug="a.md", path="a.md", meta={})
    kw = {"document": doc, "provider_registry": reg, "semantic_search_registry": ssr, "content_store": _ContentStore("hello"),
          "storage_provider": sp}

    with pytest.raises(PrimerError, match="no owner"):
        await index_document(collection=await _owned_kb(sp, _cfg("emb-priv", "ssp-pub", "ready")), **kw)
    assert emb.calls == 0

    assert await index_document(collection=await _owned_kb(sp, _cfg("emb-ada", "ssp-ada", "ready")), **kw) == 1


async def test_a_backfill_through_a_provider_no_owner_may_use_records_the_refusal(sp):
    """An admin may use any provider, but enabling search on ada's collection with one she may not use indexes nothing: the reason is
    recorded on the search block."""
    from primer.model.collection import Document

    await sp.get_storage(Document).create(Document(id="d1", collection_id="kb", slug="a.md", path="a.md", meta={}))
    await sp.get_content_store().upsert(document_id="d1", collection_id="kb", path="a.md", content="hello")
    emb, reg, ssr = _CountingEmb(), AsyncMock(), AsyncMock()
    reg.get_embedder = AsyncMock(return_value=emb)
    ssr.get_store = AsyncMock(return_value=_Store())

    updated = await enable_search(sp, reg, ssr, collection_id="kb", cfg=_cfg("emb-priv", "ssp-pub"), person=ROOT)

    assert updated.search.state == "error" and "no owner" in updated.search.error
    assert emb.calls == 0
