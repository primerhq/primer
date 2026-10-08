"""A client cannot create a system collection, or turn the flag on or off (review of #497, the bypass of the system-collection guard).

``Collection.system`` was an ordinary writable field of the generic CRUD routes. ``DELETE`` refuses a system collection (403), but
``PUT /v1/collections/{id}`` with ``"system": false`` returned 200, so a client sent that and then the DELETE, and the delete cascade
(``tests/api/test_collection_delete_cascades.py``) emptied a collection the platform regenerates. ``POST`` with ``"system": true`` returned 201
too, which let a client plant a read-only collection under an id of its choosing. The platform writes its system collections straight to
storage (``primer/internal_collections.py``, ``primer/catalog``, ``primer/knowledge/system_collection.py``), never through these routes, so the
routes now refuse any write that sets the flag on create or changes it on update. The system tools are pinned in
``tests/toolset/test_system_collection_system_flag.py``.
"""

from __future__ import annotations

import pytest

from primer.model.collection import Collection, Document
from primer.model.storage import OffsetPage
from primer.storage.q import Q

# Re-export so pytest can resolve the sqlite-backed app and client fixtures.
from tests.api.test_knowledge_documents_by_path import app, client, provider  # noqa: F401


async def _seed_system(provider, collection_id: str = "sys-1") -> None:
    """A system collection with a document, written straight to storage as the platform writes one."""
    await provider.get_storage(Collection).create(Collection(id=collection_id, description="system", system=True))
    doc = Document(id="doc-sys", collection_id=collection_id, slug="map", path="map.md", title="Map")
    await provider.get_storage(Document).create(doc)
    await provider.get_content_store().upsert(document_id=doc.id, collection_id=collection_id, path="map.md", content="regenerated")


async def _documents_of(provider, collection_id: str) -> list[str]:
    page = await provider.get_storage(Document).find(
        Q(Document).where("collection_id", collection_id).build(), OffsetPage(offset=0, length=200),
    )
    return [d.path for d in page.items]


@pytest.mark.asyncio
async def test_creating_a_collection_with_the_system_flag_is_refused(client, provider):
    resp = await client.post("/v1/collections", json={"id": "planted", "description": "x", "system": True})

    assert resp.status_code == 403, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "system flag" in resp.json()["detail"]
    assert resp.json()["extensions"]["error"] == "system_flag_protected", "a client cannot tell this 403 from any other without the code"
    assert await provider.get_storage(Collection).get("planted") is None


@pytest.mark.asyncio
async def test_a_collection_created_without_the_flag_is_a_user_collection(client, provider):
    resp = await client.post("/v1/collections", json={"id": "mine", "description": "x"})

    assert resp.status_code == 201, resp.text
    assert (await provider.get_storage(Collection).get("mine")).system is False
    explicit = await client.post("/v1/collections", json={"id": "mine-2", "description": "x", "system": False})
    assert explicit.status_code == 201, explicit.text


@pytest.mark.asyncio
async def test_turning_the_flag_off_is_refused_and_the_collection_cannot_then_be_deleted(client, provider):
    """The reported bypass, both steps: the PUT is refused, so the DELETE that follows is refused too and nothing is emptied."""
    await _seed_system(provider)

    unflag = await client.put("/v1/collections/sys-1", json={"id": "sys-1", "description": "system", "system": False})

    assert unflag.status_code == 403, unflag.text
    assert "system flag" in unflag.json()["detail"]
    assert unflag.json()["extensions"]["error"] == "system_flag_protected"
    assert (await provider.get_storage(Collection).get("sys-1")).system is True, "the flag was cleared"
    delete = await client.delete("/v1/collections/sys-1")
    assert delete.status_code == 403, delete.text
    assert await provider.get_storage(Collection).get("sys-1") is not None
    assert await _documents_of(provider, "sys-1") == ["map.md"]
    assert await provider.get_content_store().resolve_id("sys-1", "map.md") is not None


@pytest.mark.asyncio
async def test_a_put_that_leaves_out_the_flag_cannot_clear_it_either(client, provider):
    """The model default is False, so a replace that simply omits the field is a clear."""
    await _seed_system(provider)

    resp = await client.put("/v1/collections/sys-1", json={"id": "sys-1", "description": "renamed"})

    assert resp.status_code == 403, resp.text
    assert (await provider.get_storage(Collection).get("sys-1")).system is True


@pytest.mark.asyncio
async def test_turning_the_flag_on_for_a_user_collection_is_refused(client, provider):
    assert (await client.post("/v1/collections", json={"id": "mine", "description": "x"})).status_code == 201

    resp = await client.put("/v1/collections/mine", json={"id": "mine", "description": "x", "system": True})

    assert resp.status_code == 403, resp.text
    assert (await provider.get_storage(Collection).get("mine")).system is False


@pytest.mark.asyncio
async def test_an_update_that_leaves_the_flag_as_it_was_still_works(client, provider):
    """Not over-refused: a client that reads a row and writes it back keeps working, for a user collection and a system one."""
    assert (await client.post("/v1/collections", json={"id": "mine", "description": "x"})).status_code == 201
    await _seed_system(provider)

    user = await client.put("/v1/collections/mine", json={"id": "mine", "description": "edited"})
    system = await client.put("/v1/collections/sys-1", json={"id": "sys-1", "description": "edited", "system": True})

    assert user.status_code == 200, user.text
    assert system.status_code == 200, system.text
    assert (await provider.get_storage(Collection).get("mine")).description == "edited"
    assert (await provider.get_storage(Collection).get("sys-1")).system is True
