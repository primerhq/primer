"""The system collection tools cannot create a system collection, or turn the flag on or off (review of #497).

The tool half of ``tests/api/test_collection_system_flag.py``: ``update_collection`` with ``system: false`` was accepted, after which
``delete_collection`` (which refuses a system collection) deleted it and the cascade emptied it. The tools answer ``type=forbidden``,
as they do for the other rules the platform owns (a reserved id, a system collection's delete).
"""

from __future__ import annotations

import pytest

from primer.model.collection import Collection, Document
from primer.model.storage import OffsetPage
from primer.storage.q import Q

# Re-export so pytest can resolve the real-sqlite system toolset: "kb-1" is a user collection and "sys-1" a system one (written to storage).
from tests.toolset.test_system_document_tools import _call, world  # noqa: F401


async def _documents_of(sp, collection_id: str) -> list[str]:
    page = await sp.get_storage(Document).find(
        Q(Document).where("collection_id", collection_id).build(), OffsetPage(offset=0, length=200),
    )
    return [d.path for d in page.items]


@pytest.mark.asyncio
async def test_creating_a_collection_with_the_system_flag_is_refused(world) -> None:
    sp, toolset, _ = world

    is_error, body = await _call(toolset, "create_collection", entity={"id": "planted", "description": "x", "system": True})

    assert is_error and body["type"] == "forbidden", body
    assert "system flag" in body["message"]
    assert await sp.get_storage(Collection).get("planted") is None


@pytest.mark.asyncio
async def test_turning_the_flag_off_is_refused_and_the_collection_cannot_then_be_deleted(world) -> None:
    """The reported bypass, both steps."""
    sp, toolset, _ = world
    doc = Document(id="doc-sys", collection_id="sys-1", slug="map", path="map.md", title="Map")
    await sp.get_storage(Document).create(doc)
    await sp.get_content_store().upsert(document_id=doc.id, collection_id="sys-1", path="map.md", content="regenerated")

    is_error, body = await _call(toolset, "update_collection", id="sys-1", entity={"id": "sys-1", "description": "system", "system": False})

    assert is_error and body["type"] == "forbidden", body
    assert "system flag" in body["message"]
    assert (await sp.get_storage(Collection).get("sys-1")).system is True, "the flag was cleared"
    is_error, body = await _call(toolset, "delete_collection", id="sys-1")
    assert is_error and body["type"] == "forbidden", body
    assert await sp.get_storage(Collection).get("sys-1") is not None
    assert await _documents_of(sp, "sys-1") == ["map.md"]
    assert await sp.get_content_store().resolve_id("sys-1", "map.md") is not None


@pytest.mark.asyncio
async def test_an_update_that_omits_the_flag_cannot_clear_it_either(world) -> None:
    sp, toolset, _ = world

    is_error, body = await _call(toolset, "update_collection", id="sys-1", entity={"id": "sys-1", "description": "renamed"})

    assert is_error and body["type"] == "forbidden", body
    assert (await sp.get_storage(Collection).get("sys-1")).system is True


@pytest.mark.asyncio
async def test_turning_the_flag_on_for_a_user_collection_is_refused(world) -> None:
    sp, toolset, _ = world

    is_error, body = await _call(toolset, "update_collection", id="kb-1", entity={"id": "kb-1", "description": "kb", "system": True})

    assert is_error and body["type"] == "forbidden", body
    assert (await sp.get_storage(Collection).get("kb-1")).system is False


@pytest.mark.asyncio
async def test_an_update_that_leaves_the_flag_as_it_was_still_works(world) -> None:
    sp, toolset, _ = world
    kb = (await sp.get_storage(Collection).get("kb-1")).model_dump(mode="json")

    is_error, body = await _call(toolset, "update_collection", id="kb-1", entity={**kb, "description": "edited"})
    assert not is_error, body

    assert (await sp.get_storage(Collection).get("kb-1")).description == "edited"
    is_error, body = await _call(toolset, "update_collection", id="sys-1", entity={"id": "sys-1", "description": "edited", "system": True})
    assert not is_error, body
    assert (await sp.get_storage(Collection).get("sys-1")).system is True


@pytest.mark.asyncio
async def test_a_collection_created_without_the_flag_is_a_user_collection(world) -> None:
    sp, toolset, _ = world

    is_error, body = await _call(toolset, "create_collection", entity={"id": "mine", "description": "x"})

    assert not is_error, body
    assert (await sp.get_storage(Collection).get("mine")).system is False
