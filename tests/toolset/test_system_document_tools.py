"""The system ``create_document`` / ``update_document`` / ``delete_document`` tools keep a document whole (task 01a111d1, D3).

A document is two rows that must stay in lockstep: the entity row and the content row (the body, authoritative for path
resolution), plus its chunks in the vector store. The generic CRUD tools wrote only the entity row, which, on a real sqlite store
(the probe behind this file), left an agent-created document unreadable, unlisted and unsearchable; deleted a real document's
entity while its content row and chunks stayed (a ghost still listed and searchable, and a path ``put_document`` could never
write again: it resolved the old id and updated a deleted entity); and accepted writes into system collections and onto
harness-managed rows, which the REST routes refuse. Ruling (the lead, Option A): the three tools delegate to
:class:`~primer.knowledge.tree.DocumentTreeService`, the service the collections toolset and the REST tree routes use.

* ``create_document`` makes a real, readable, empty document. The caller's ``id`` is IGNORED (the service mints it) and the minted one
  is returned; content goes in through ``put_document``.
* ``update_document`` changes title and meta only; a changed path / slug / parent_id / collection_id is refused ("use move_document").
* all three refuse system collections and harness-managed rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.api.registries import ProviderRegistry
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig, Document
from primer.model.common import dump_for_storage
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.system import build_system_toolset
from tests.toolset.test_system import _emb, _FakeEmbedder


class _RecordingStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str], object] = {}

    async def create_collection(self, collection_id, *, dimensions, distance="cosine"):
        return None

    async def drop_collection(self, collection_id):
        return None

    async def put(self, record):
        self.records[(record.collection_id, record.document_id, record.chunk_id)] = record

    async def delete(self, collection_id, document_id):
        for key in [k for k in self.records if k[0] == collection_id and k[1] == document_id]:
            del self.records[key]

    async def search(self, collection_id, vector, k):
        return []

    def doc_ids(self) -> set[str]:
        return {k[1] for k in self.records}


class _SSR:
    is_configured = True

    def __init__(self, store) -> None:
        self._store = store

    async def get_store(self, ssp_id):
        return self._store

    async def aclose(self):
        return None


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    store = _RecordingStore()
    registry = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: _FakeEmbedder([0.1, 0.2, 0.3, 0.4]),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    toolset = build_system_toolset(storage_provider=sp, provider_registry=registry, semantic_search_registry=_SSR(store))
    registry._system_toolset_provider = toolset
    await toolset.call(tool_name="create_embedding_provider", arguments={"entity": dump_for_storage(_emb())})
    kb = Collection(
        id="kb-1", description="kb",
        search=CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="hf-1", model="sentence-transformers/all-MiniLM-L6-v2"),
            vector_store_provider_id="ssp-1",
        ),
    )
    r = await toolset.call(tool_name="create_collection", arguments={"entity": kb.model_dump(mode="json")})
    assert not r.is_error, r.output
    # A system collection is written straight to storage, as the platform does: the tool refuses to create one.
    await sp.get_storage(Collection).create(Collection(id="sys-1", description="system", system=True))
    yield sp, toolset, store
    await sp.aclose()


async def _call(toolset, name: str, **args):
    result = await toolset.call(tool_name=name, arguments=args)
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


def _entity(**fields) -> dict:
    base = {"id": "caller-chosen-id", "collection_id": "kb-1", "slug": "notes", "path": "notes", "title": "Notes"}
    base.update(fields)
    return base


async def _put(toolset, path: str, content: str = "a body worth indexing"):
    is_error, body = await _call(toolset, "put_document", collection_id="kb-1", path=path, content=content)
    assert not is_error, body
    return body["id"]


async def _seed_managed(sp, path: str = "managed") -> str:
    """A harness-managed document, written straight to storage (the REST and tool paths both refuse to create one)."""
    doc = Document(id="doc-managed", collection_id="kb-1", slug=path, path=path, title="M", harness_id="hns_x")
    await sp.get_storage(Document).create(doc)
    await sp.get_content_store().upsert(document_id=doc.id, collection_id="kb-1", path=path, content="managed body")
    return doc.id


class TestCreateDocumentMakesARealDocument:
    @pytest.mark.asyncio
    async def test_the_new_document_is_readable_and_listed_by_the_path_tools(self, world) -> None:
        sp, toolset, store = world

        is_error, created = await _call(toolset, "create_document", entity=_entity())

        assert not is_error, created
        is_error, read = await _call(toolset, "get_document_content", collection_id="kb-1", path="notes")
        assert not is_error, f"an agent-created document is unreadable: {read}"
        assert read["content"] == "" and read["id"] == created["id"]
        _, listed = await _call(toolset, "list_documents", collection_id="kb-1")
        assert [(d["path"], d["document_id"]) for d in listed["documents"]] == [("notes", created["id"])]

    @pytest.mark.asyncio
    async def test_the_callers_id_is_ignored_and_the_minted_one_is_returned(self, world) -> None:
        _, toolset, _ = world

        _, created = await _call(toolset, "create_document", entity=_entity())

        assert created["id"] != "caller-chosen-id"
        is_error, _ = await _call(toolset, "get_document", id="caller-chosen-id")
        assert is_error, "a row exists under the id the service was supposed to ignore"
        is_error, row = await _call(toolset, "get_document", id=created["id"])
        assert not is_error and row["path"] == "notes"

    @pytest.mark.asyncio
    async def test_meta_is_stored_on_the_new_document(self, world) -> None:
        _, toolset, _ = world

        is_error, created = await _call(toolset, "create_document", entity=_entity(meta={"owner": "ops"}))

        assert not is_error, created
        _, row = await _call(toolset, "get_document", id=created["id"])
        assert row["meta"] == {"owner": "ops"}

    @pytest.mark.asyncio
    async def test_a_parent_id_nests_the_path(self, world) -> None:
        _, toolset, _ = world
        _, parent = await _call(toolset, "create_document", entity=_entity(slug="guides", path="guides", title="G"))

        is_error, child = await _call(
            toolset, "create_document", entity=_entity(id="x", slug="setup", path="guides/setup", parent_id=parent["id"]),
        )

        assert not is_error, child
        assert child["path"] == "guides/setup" and child["parent_id"] == parent["id"]
        is_error, _ = await _call(toolset, "get_document_content", collection_id="kb-1", path="guides/setup")
        assert not is_error

    @pytest.mark.asyncio
    async def test_a_path_that_disagrees_with_parent_and_slug_is_refused(self, world) -> None:
        _, toolset, _ = world

        is_error, body = await _call(toolset, "create_document", entity=_entity(path="somewhere/else"))

        assert is_error and body["type"] == "bad-request"

    @pytest.mark.asyncio
    async def test_a_slug_outside_the_strict_charset_is_refused(self, world) -> None:
        _, toolset, _ = world

        is_error, body = await _call(toolset, "create_document", entity=_entity(slug="Notes", path="Notes"))

        assert is_error and body["type"] in ("bad-request", "validation-error")

    @pytest.mark.asyncio
    async def test_a_second_create_at_the_same_path_is_a_conflict(self, world) -> None:
        _, toolset, _ = world
        await _call(toolset, "create_document", entity=_entity())

        is_error, body = await _call(toolset, "create_document", entity=_entity(id="another"))

        assert is_error and body["type"] == "conflict"

    @pytest.mark.asyncio
    async def test_a_missing_collection_or_parent_is_not_found(self, world) -> None:
        _, toolset, _ = world

        no_coll_error, no_coll = await _call(toolset, "create_document", entity=_entity(collection_id="nope"))
        no_parent_error, no_parent = await _call(toolset, "create_document", entity=_entity(parent_id="ghost", path="ghost/notes"))

        assert no_coll_error and no_coll["type"] == "not-found"
        assert no_parent_error and no_parent["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_a_system_collection_is_refused(self, world) -> None:
        sp, toolset, _ = world

        is_error, body = await _call(toolset, "create_document", entity=_entity(collection_id="sys-1"))

        assert is_error and body["type"] == "forbidden"
        assert await sp.get_content_store().get_by_path("sys-1", "notes") is None

    @pytest.mark.asyncio
    async def test_a_body_that_sets_harness_id_is_refused(self, world) -> None:
        _, toolset, _ = world

        is_error, body = await _call(toolset, "create_document", entity=_entity(harness_id="hns_x"))

        assert is_error and body["type"] in ("bad-request", "conflict", "forbidden")


class TestUpdateDocumentChangesTitleAndMetaOnly:
    @pytest.mark.asyncio
    async def test_title_and_meta_are_applied_and_the_body_is_untouched(self, world) -> None:
        _, toolset, _ = world
        doc_id = await _put(toolset, "page", "the original body")
        _, row = await _call(toolset, "get_document", id=doc_id)
        row.update(title="Renamed", meta={"owner": "ops"})

        is_error, updated = await _call(toolset, "update_document", id=doc_id, entity=row)

        assert not is_error, updated
        assert updated["title"] == "Renamed" and updated["meta"] == {"owner": "ops"}
        _, read = await _call(toolset, "get_document_content", collection_id="kb-1", path="page")
        assert read["content"] == "the original body" and read["title"] == "Renamed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change",
        [{"path": "elsewhere"}, {"slug": "other"}, {"parent_id": "some-parent"}, {"collection_id": "sys-1"}],
        ids=["path", "slug", "parent_id", "collection_id"],
    )
    async def test_a_change_of_identity_is_refused_and_points_at_move_document(self, world, change) -> None:
        _, toolset, _ = world
        doc_id = await _put(toolset, "page")
        _, row = await _call(toolset, "get_document", id=doc_id)
        row.update(change)

        is_error, body = await _call(toolset, "update_document", id=doc_id, entity=row)

        assert is_error and body["type"] == "bad-request"
        assert "move_document" in body["message"]
        _, read = await _call(toolset, "get_document_content", collection_id="kb-1", path="page")
        assert read["path"] == "page", "the entity path mirror drifted from the content row"

    @pytest.mark.asyncio
    async def test_a_harness_managed_row_is_refused(self, world) -> None:
        sp, toolset, _ = world
        doc_id = await _seed_managed(sp)
        _, row = await _call(toolset, "get_document", id=doc_id)
        row["title"] = "edited"

        is_error, body = await _call(toolset, "update_document", id=doc_id, entity=row)

        assert is_error and body["type"] == "conflict"
        assert (await sp.get_storage(Document).get(doc_id)).title == "M"

    @pytest.mark.asyncio
    async def test_setting_harness_id_on_an_unmanaged_row_is_refused(self, world) -> None:
        _, toolset, _ = world
        doc_id = await _put(toolset, "page")
        _, row = await _call(toolset, "get_document", id=doc_id)
        row["harness_id"] = "hns_x"

        is_error, body = await _call(toolset, "update_document", id=doc_id, entity=row)

        assert is_error and body["type"] in ("bad-request", "conflict", "forbidden")

    @pytest.mark.asyncio
    async def test_a_document_in_a_system_collection_is_refused(self, world) -> None:
        sp, toolset, _ = world
        doc = Document(id="doc-sys", collection_id="sys-1", slug="s", path="s", title="S")
        await sp.get_storage(Document).create(doc)
        await sp.get_content_store().upsert(document_id=doc.id, collection_id="sys-1", path="s", content="x")
        _, row = await _call(toolset, "get_document", id="doc-sys")
        row["title"] = "edited"

        is_error, body = await _call(toolset, "update_document", id="doc-sys", entity=row)

        assert is_error and body["type"] == "forbidden"

    @pytest.mark.asyncio
    async def test_an_unknown_id_is_not_found_and_a_body_id_mismatch_is_a_conflict(self, world) -> None:
        _, toolset, _ = world
        doc_id = await _put(toolset, "page")
        _, row = await _call(toolset, "get_document", id=doc_id)

        unknown_error, unknown = await _call(toolset, "update_document", id="nope", entity={**row, "id": "nope"})
        mismatch_error, mismatch = await _call(toolset, "update_document", id=doc_id, entity={**row, "id": "other"})

        assert unknown_error and unknown["type"] == "not-found"
        assert mismatch_error and mismatch["type"] == "conflict"


class TestDeleteDocumentRemovesTheWholeDocument:
    @pytest.mark.asyncio
    async def test_the_content_row_and_the_chunks_go_with_the_entity(self, world) -> None:
        sp, toolset, store = world
        doc_id = await _put(toolset, "page")
        assert doc_id in store.doc_ids(), "precondition: the document was indexed"

        is_error, body = await _call(toolset, "delete_document", id=doc_id)

        assert not is_error, body
        is_error, read = await _call(toolset, "get_document_content", collection_id="kb-1", path="page")
        assert is_error and "missing" not in str(read), f"a ghost is left behind: {read}"
        _, listed = await _call(toolset, "list_documents", collection_id="kb-1")
        assert listed["documents"] == [], "the deleted document is still listed"
        assert doc_id not in store.doc_ids(), "the deleted document is still searchable"

    @pytest.mark.asyncio
    async def test_the_path_can_be_written_again_afterwards(self, world) -> None:
        """The wedge: the old delete left the content row, so ``put_document`` resolved the old id, tried to update a deleted
        entity and failed for ever."""
        _, toolset, _ = world
        doc_id = await _put(toolset, "page", "first")
        await _call(toolset, "delete_document", id=doc_id)

        is_error, body = await _call(toolset, "put_document", collection_id="kb-1", path="page", content="second")

        assert not is_error, f"the path is wedged: {body}"
        _, read = await _call(toolset, "get_document_content", collection_id="kb-1", path="page")
        assert read["content"] == "second"

    @pytest.mark.asyncio
    async def test_a_document_with_children_is_a_conflict_and_nothing_is_deleted(self, world) -> None:
        _, toolset, _ = world
        _, parent = await _call(toolset, "create_document", entity=_entity(slug="guides", path="guides"))
        await _call(toolset, "create_document", entity=_entity(id="x", slug="setup", path="guides/setup", parent_id=parent["id"]))

        is_error, body = await _call(toolset, "delete_document", id=parent["id"])

        assert is_error and body["type"] == "conflict"
        _, listed = await _call(toolset, "list_documents", collection_id="kb-1")
        assert {d["path"] for d in listed["documents"]} == {"guides", "guides/setup"}

    @pytest.mark.asyncio
    async def test_a_harness_managed_row_is_refused(self, world) -> None:
        sp, toolset, _ = world
        doc_id = await _seed_managed(sp)

        is_error, body = await _call(toolset, "delete_document", id=doc_id)

        assert is_error and body["type"] == "conflict"
        assert await sp.get_storage(Document).get(doc_id) is not None

    @pytest.mark.asyncio
    async def test_a_document_in_a_system_collection_is_refused(self, world) -> None:
        sp, toolset, _ = world
        doc = Document(id="doc-sys", collection_id="sys-1", slug="s", path="s", title="S")
        await sp.get_storage(Document).create(doc)
        await sp.get_content_store().upsert(document_id=doc.id, collection_id="sys-1", path="s", content="x")

        is_error, body = await _call(toolset, "delete_document", id="doc-sys")

        assert is_error and body["type"] == "forbidden"
        assert await sp.get_storage(Document).get("doc-sys") is not None

    @pytest.mark.asyncio
    async def test_an_unknown_id_is_not_found(self, world) -> None:
        _, toolset, _ = world

        is_error, body = await _call(toolset, "delete_document", id="nope")

        assert is_error and body["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_a_ghost_that_shares_a_path_with_a_real_document_leaves_the_real_one_alone(self, world) -> None:
        """The old create_document left entity rows with no content row; a later put_document at the same path made a real
        document beside such a ghost. Deleting the ghost by id must not resolve the PATH and delete the real document."""
        sp, toolset, _ = world
        real_id = await _put(toolset, "raw", "the real body")
        ghost = Document(id="doc-ghost", collection_id="kb-1", slug="raw", path="raw", title="Ghost")
        await sp.get_storage(Document).create(ghost)

        is_error, body = await _call(toolset, "delete_document", id="doc-ghost")

        assert not is_error, body
        assert await sp.get_storage(Document).get("doc-ghost") is None
        assert await sp.get_storage(Document).get(real_id) is not None, "the real document was deleted instead of the ghost"
        _, read = await _call(toolset, "get_document_content", collection_id="kb-1", path="raw")
        assert read["content"] == "the real body"

    @pytest.mark.asyncio
    async def test_an_entity_the_old_tool_left_without_a_content_row_can_still_be_deleted(self, world) -> None:
        """Clean-up of what the old create_document wrote: an entity row nothing can read, list or search."""
        sp, toolset, _ = world
        ghost = Document(id="doc-ghost", collection_id="kb-1", slug="ghost", path="ghost", title="Ghost")
        await sp.get_storage(Document).create(ghost)

        is_error, body = await _call(toolset, "delete_document", id="doc-ghost")

        assert not is_error, body
        assert await sp.get_storage(Document).get("doc-ghost") is None
