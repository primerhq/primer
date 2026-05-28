"""Tests for make_crud_router managed_by_field param.

Verifies that when ``managed_by_field`` is set the router auto-wires:
- CREATE: 422 when body sets the managed field
- UPDATE: 409 when existing row has managed field set
- DELETE: 409 when existing row has managed field set
- Unmanaged rows pass through normally
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
from matrix.model.storage import OffsetPage, OffsetPageResponse


# ---------------------------------------------------------------------------
# Tiny model under test
# ---------------------------------------------------------------------------


class _Item(BaseModel):
    id: str
    name: str = ""
    harness_id: str | None = None


# ---------------------------------------------------------------------------
# In-memory storage for _Item
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
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        return OffsetPageResponse(offset=0, length=len(items), total=len(items), items=items)

    async def find(self, predicate: Any, page: Any, *, order_by: Any = None) -> OffsetPageResponse[_Item]:
        return await self.list(page, order_by=order_by)


# ---------------------------------------------------------------------------
# Shared storage singleton + dependency
# ---------------------------------------------------------------------------

_SHARED_STORAGE: _ItemStorage | None = None


def _get_item_storage() -> _ItemStorage:
    assert _SHARED_STORAGE is not None
    return _SHARED_STORAGE


# ---------------------------------------------------------------------------
# Fixture: managed app + client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def managed_client() -> AsyncIterator[httpx.AsyncClient]:
    global _SHARED_STORAGE  # noqa: PLW0603

    _SHARED_STORAGE = _ItemStorage(
        items=[
            _Item(id="free-item", name="unmanaged"),
            _Item(id="managed-item", name="managed", harness_id="h1"),
        ]
    )

    router = make_crud_router(
        model_cls=_Item,
        storage_dep=_get_item_storage,
        plural="items",
        tag="items",
        managed_by_field="harness_id",
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
async def test_create_with_managed_field_in_body_rejected(managed_client: httpx.AsyncClient) -> None:
    """Creating an entity with harness_id set in the body should return 422."""
    resp = await managed_client.post("/v1/items", json={"id": "x", "harness_id": "h1"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_without_managed_field_succeeds(managed_client: httpx.AsyncClient) -> None:
    """Creating an entity without harness_id set in the body should succeed."""
    resp = await managed_client.post("/v1/items", json={"id": "new-item", "name": "fresh"})
    assert resp.status_code == 201
    assert resp.json()["id"] == "new-item"
    assert resp.json()["harness_id"] is None


@pytest.mark.asyncio
async def test_update_managed_row_rejected(managed_client: httpx.AsyncClient) -> None:
    """PUT on a row whose harness_id is set should return 409."""
    resp = await managed_client.put(
        "/v1/items/managed-item",
        json={"id": "managed-item", "name": "new", "harness_id": "h1"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_managed_row_rejected(managed_client: httpx.AsyncClient) -> None:
    """DELETE on a row whose harness_id is set should return 409."""
    resp = await managed_client.delete("/v1/items/managed-item")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_unmanaged_row_update_works_normally(managed_client: httpx.AsyncClient) -> None:
    """PUT on a row without harness_id should succeed."""
    resp = await managed_client.put(
        "/v1/items/free-item",
        json={"id": "free-item", "name": "ok"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "ok"


@pytest.mark.asyncio
async def test_unmanaged_row_delete_works_normally(managed_client: httpx.AsyncClient) -> None:
    """DELETE on a row without harness_id should succeed."""
    resp = await managed_client.delete("/v1/items/free-item")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_create_managed_field_none_is_allowed(managed_client: httpx.AsyncClient) -> None:
    """Creating an entity with harness_id explicitly null is fine."""
    resp = await managed_client.post("/v1/items", json={"id": "y", "harness_id": None})
    assert resp.status_code == 201
