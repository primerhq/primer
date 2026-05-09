"""Unit tests for matrix.api.registries.vector_store_registry.VectorStoreRegistry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from matrix.api.registries.vector_store_registry import (
    ACTIVE_VECTOR_STORE_CONFIG_ID,
    VectorStoreRegistry,
)
from matrix.model.except_ import ConfigError
from matrix.model.vector import VectorStoreConfig


class _FakeStorage:
    def __init__(self) -> None:
        self._data = {}

    async def get(self, id: str):
        return self._data.get(id)

    async def create(self, entity):
        self._data[entity.id] = entity
        return entity


class _FakeStorageProvider:
    def __init__(self) -> None:
        self._stores = {}

    def get_storage(self, model_class):
        return self._stores.setdefault(model_class, _FakeStorage())

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


class TestGetWithoutConfig:
    @pytest.mark.asyncio
    async def test_get_raises_when_no_config_row(self) -> None:
        sp = _FakeStorageProvider()
        registry = VectorStoreRegistry(sp, factory=lambda c: MagicMock())
        with pytest.raises(ConfigError, match="no vector store"):
            await registry.get()


class TestGetWithConfig:
    @pytest.mark.asyncio
    async def test_get_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(VectorStoreConfig).create(
            VectorStoreConfig(
                id=ACTIVE_VECTOR_STORE_CONFIG_ID,
                backend="pgvector",
                settings={"dsn": "x"},
            )
        )
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        store_mock = MagicMock()
        provider_mock.get_vector_store = MagicMock(return_value=store_mock)

        factory = MagicMock(return_value=provider_mock)
        registry = VectorStoreRegistry(sp, factory=factory)

        first = await registry.get()
        second = await registry.get()
        assert first is second
        assert factory.call_count == 1
        provider_mock.initialize.assert_awaited_once()


class TestGetProvider:
    @pytest.mark.asyncio
    async def test_returns_underlying_provider_after_first_get(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(VectorStoreConfig).create(
            VectorStoreConfig(
                id=ACTIVE_VECTOR_STORE_CONFIG_ID,
                backend="pgvector",
                settings={"dsn": "x"},
            )
        )
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        provider_mock.get_vector_store = MagicMock(return_value=MagicMock())

        registry = VectorStoreRegistry(sp, factory=lambda c: provider_mock)
        provider = await registry.get_provider()
        assert provider is provider_mock


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_closes_provider(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(VectorStoreConfig).create(
            VectorStoreConfig(
                id=ACTIVE_VECTOR_STORE_CONFIG_ID,
                backend="pgvector",
                settings={"dsn": "x"},
            )
        )
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        provider_mock.get_vector_store = MagicMock(return_value=MagicMock())

        registry = VectorStoreRegistry(sp, factory=lambda c: provider_mock)
        await registry.get()
        await registry.invalidate()
        provider_mock.aclose.assert_awaited_once()


class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_closes_provider_when_present(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(VectorStoreConfig).create(
            VectorStoreConfig(
                id=ACTIVE_VECTOR_STORE_CONFIG_ID,
                backend="pgvector",
                settings={"dsn": "x"},
            )
        )
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        provider_mock.get_vector_store = MagicMock(return_value=MagicMock())

        registry = VectorStoreRegistry(sp, factory=lambda c: provider_mock)
        await registry.get()
        await registry.aclose()
        provider_mock.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aclose_noop_when_never_initialised(self) -> None:
        sp = _FakeStorageProvider()
        registry = VectorStoreRegistry(sp, factory=lambda c: MagicMock())
        await registry.aclose()
