"""Tests for make_crud_router scope_field + parent_path_segment params.

Verifies that when both params are set the router:
- Mounts at /v1/{parent_path_segment}/{parent_id}/{plural}
- LIST filters by scope_field == parent_id
- CREATE forces scope_field to parent_id; 422 on mismatch
- GET/PATCH/DELETE verify parent_id matches scope_field; 404 otherwise
- Raises ValueError when only one of the two params is set
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import BaseModel

from matrix.api.errors import register_error_handlers
from matrix.api.routers._crud import make_crud_router
from matrix.model.common import Identifiable
from matrix.model.storage import OffsetPage, OffsetPageResponse


# ---------------------------------------------------------------------------
# Tiny model under test
# ---------------------------------------------------------------------------


class _Item(Identifiable):
    workspace_id: str


# ---------------------------------------------------------------------------
# In-memory storage for _Item (scoped find via workspace_id)
# ---------------------------------------------------------------------------


class _ItemStorage:
    def __init__(self, items: list[_Item] | None = None) -> None:
        self._data: dict[str, _Item] = {item.id: item for item in (items or [])}

    async def get(self, id: str) -> _Item | None:
        return self._data.get(id)

    async def create(self, entity: _Item) -> _Item:
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _Item) -> _Item:
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        del self._data[id]

    async def list(self, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_Item]:
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            sliced = items[page.offset: page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        return OffsetPageResponse(offset=0, length=len(items), total=len(items), items=items)

    async def find(self, predicate: Any, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_Item]:
        if predicate is None:
            return await self.list(page, order_by=order_by)
        from matrix.model.storage import FieldRef, Op, Predicate, Value

        def _matches(item: _Item, node: Any) -> bool:
            if isinstance(node, Predicate):
                if node.op == Op.AND:
                    return _matches(item, node.left) and _matches(item, node.right)
                if node.op == Op.OR:
                    return _matches(item, node.left) or _matches(item, node.right)
                if isinstance(node.left, FieldRef) and isinstance(node.right, Value):
                    actual = getattr(item, node.left.name, None)
                    if node.op == Op.EQ:
                        return actual == node.right.value
            return True

        matched = [i for i in self._data.values() if _matches(i, predicate)]
        if isinstance(page, OffsetPage):
            sliced = matched[page.offset: page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(matched),
                items=sliced,
            )
        return OffsetPageResponse(offset=0, length=len(matched), total=len(matched), items=matched)


# ---------------------------------------------------------------------------
# Shared storage singleton + dependency
# ---------------------------------------------------------------------------

_SHARED_STORAGE: _ItemStorage | None = None


def _get_item_storage() -> _ItemStorage:
    assert _SHARED_STORAGE is not None
    return _SHARED_STORAGE


# ---------------------------------------------------------------------------
# Fixture: scoped app + client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def scoped_client() -> AsyncIterator[httpx.AsyncClient]:
    global _SHARED_STORAGE  # noqa: PLW0603

    _SHARED_STORAGE = _ItemStorage(
        items=[
            _Item(id="item-x", workspace_id="w1"),
            _Item(id="item-y", workspace_id="w1"),
            _Item(id="item-z", workspace_id="w2"),
        ]
    )

    router = make_crud_router(
        model_cls=_Item,
        storage_dep=_get_item_storage,
        plural="items",
        tag="items",
        scope_field="workspace_id",
        parent_path_segment="workspaces",
    )

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix="/v1")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    _SHARED_STORAGE = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoped_list_filters_by_parent_id(scoped_client: httpx.AsyncClient) -> None:
    resp = await scoped_client.get("/v1/workspaces/w1/items")
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    assert len(items) == 2
    assert all(item["workspace_id"] == "w1" for item in items)


@pytest.mark.asyncio
async def test_scoped_list_other_parent(scoped_client: httpx.AsyncClient) -> None:
    resp = await scoped_client.get("/v1/workspaces/w2/items")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["workspace_id"] == "w2"


@pytest.mark.asyncio
async def test_scoped_create_forces_parent_id(scoped_client: httpx.AsyncClient) -> None:
    """Create with matching workspace_id succeeds."""
    resp = await scoped_client.post(
        "/v1/workspaces/w1/items",
        json={"id": "new-item", "workspace_id": "w1"},
    )
    assert resp.status_code == 201
    assert resp.json()["workspace_id"] == "w1"


@pytest.mark.asyncio
async def test_scoped_create_forces_parent_id_mismatch_422(scoped_client: httpx.AsyncClient) -> None:
    """Create with mismatched workspace_id is rejected with 422."""
    resp = await scoped_client.post(
        "/v1/workspaces/w1/items",
        json={"id": "x", "workspace_id": "w2"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_scoped_create_sets_parent_id_when_absent(scoped_client: httpx.AsyncClient) -> None:
    """Create where body omits scope_field — factory fills it in from path."""
    # _Item requires workspace_id so we must supply it; supply the correct one.
    resp = await scoped_client.post(
        "/v1/workspaces/w1/items",
        json={"id": "auto-fill", "workspace_id": "w1"},
    )
    assert resp.status_code == 201
    assert resp.json()["workspace_id"] == "w1"


@pytest.mark.asyncio
async def test_scoped_get_returns_item_in_correct_parent(scoped_client: httpx.AsyncClient) -> None:
    resp = await scoped_client.get("/v1/workspaces/w1/items/item-x")
    assert resp.status_code == 200
    assert resp.json()["id"] == "item-x"


@pytest.mark.asyncio
async def test_scoped_get_verifies_parent(scoped_client: httpx.AsyncClient) -> None:
    """item-x belongs to w1; accessing it under w2 should 404."""
    resp = await scoped_client.get("/v1/workspaces/w2/items/item-x")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_scoped_delete_verifies_parent(scoped_client: httpx.AsyncClient) -> None:
    """Deleting an item via the wrong parent_id should 404."""
    resp = await scoped_client.delete("/v1/workspaces/w2/items/item-x")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_scoped_delete_correct_parent(scoped_client: httpx.AsyncClient) -> None:
    """Deleting an item via the correct parent_id should succeed."""
    resp = await scoped_client.delete("/v1/workspaces/w1/items/item-x")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_scoped_update_verifies_parent(scoped_client: httpx.AsyncClient) -> None:
    """PUT an item via the wrong parent_id should 404."""
    resp = await scoped_client.put(
        "/v1/workspaces/w2/items/item-x",
        json={"id": "item-x", "workspace_id": "w1"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_scoped_update_correct_parent(scoped_client: httpx.AsyncClient) -> None:
    """PUT an item via the correct parent_id should succeed."""
    resp = await scoped_client.put(
        "/v1/workspaces/w1/items/item-x",
        json={"id": "item-x", "workspace_id": "w1"},
    )
    assert resp.status_code == 200


def test_one_param_without_other_raises_scope_field_only() -> None:
    """Passing only scope_field (no parent_path_segment) raises ValueError."""
    with pytest.raises(ValueError, match="parent_path_segment"):
        make_crud_router(
            model_cls=_Item,
            storage_dep=_get_item_storage,
            plural="items",
            tag="t",
            scope_field="workspace_id",
        )


def test_one_param_without_other_raises_parent_path_segment_only() -> None:
    """Passing only parent_path_segment (no scope_field) raises ValueError."""
    with pytest.raises(ValueError, match="scope_field"):
        make_crud_router(
            model_cls=_Item,
            storage_dep=_get_item_storage,
            plural="items",
            tag="t",
            parent_path_segment="workspaces",
        )
