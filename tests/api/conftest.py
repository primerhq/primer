"""Shared fixtures for the FastAPI test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from matrix.api.app import create_test_app
from matrix.api.registries import ProviderRegistry, VectorStoreRegistry
from matrix.model.common import Identifiable
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.storage import (
    CursorPage,
    CursorPageResponse,
    OffsetPage,
    OffsetPageResponse,
)


_T = TypeVar("_T", bound=Identifiable)


class _InMemoryStorage(Generic[_T]):
    """Bare-bones in-memory ``Storage[T]`` for tests."""

    def __init__(self, model_cls: type[_T]) -> None:
        self._cls = model_cls
        self._data: dict[str, _T] = {}

    async def get(self, id: str) -> _T | None:
        return self._data.get(id)

    async def create(self, entity: _T) -> _T:
        if entity.id in self._data:
            raise ConflictError(f"id {entity.id!r} already exists")
        self._data[entity.id] = entity
        return entity

    async def update(self, entity: _T) -> _T:
        if entity.id not in self._data:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            sliced = items[page.offset : page.offset + page.length]
            return OffsetPageResponse(
                offset=page.offset,
                length=len(sliced),
                total=len(items),
                items=sliced,
            )
        offset = int(page.cursor) if page.cursor else 0
        sliced = items[offset : offset + page.length]
        next_cursor: str | None = None
        if offset + page.length < len(items):
            next_cursor = str(offset + page.length)
        return CursorPageResponse(next_cursor=next_cursor, items=sliced)

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _FakeStorageProvider:
    """In-memory ``StorageProvider`` returning ``_InMemoryStorage`` per model."""

    def __init__(self) -> None:
        self._stores: dict[type, _InMemoryStorage[Any]] = {}

    def get_storage(self, model_class: type[_T]) -> _InMemoryStorage[_T]:
        return self._stores.setdefault(model_class, _InMemoryStorage(model_class))

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


@pytest.fixture
def fake_storage_provider() -> _FakeStorageProvider:
    return _FakeStorageProvider()


@pytest.fixture
def fake_provider_registry(
    fake_storage_provider: _FakeStorageProvider,
) -> ProviderRegistry:
    return ProviderRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )


@pytest.fixture
def fake_vector_store_registry(
    fake_storage_provider: _FakeStorageProvider,
) -> VectorStoreRegistry:
    return VectorStoreRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        factory=lambda c: object(),  # type: ignore[arg-type]
    )


@pytest.fixture
def app(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    fake_vector_store_registry: VectorStoreRegistry,
) -> FastAPI:
    return create_test_app(
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
        provider_registry=fake_provider_registry,
        vector_store_registry=fake_vector_store_registry,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


__all__ = [
    "_FakeStorageProvider",
    "_InMemoryStorage",
]
