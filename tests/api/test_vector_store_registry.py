"""Unit tests for matrix.api.registries.vector_store_registry.VectorStoreRegistry.

The registry now reads configuration directly from the AppConfig
(``vector_store`` field) rather than from storage. ``None`` config
disables the subsystem; a present config builds the provider lazily
on first access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from matrix.api.registries.vector_store_registry import VectorStoreRegistry
from matrix.model.except_ import ConfigError
from matrix.model.provider import (
    PgVectorConfig,
    VectorStoreProviderConfig,
    VectorStoreProviderType,
)


def _config() -> VectorStoreProviderConfig:
    return VectorStoreProviderConfig(
        provider=VectorStoreProviderType.PGVECTOR,
        config=PgVectorConfig(
            hostname="x",
            username="u",
            password="p",  # type: ignore[arg-type]
            database="d",
        ),
    )


class TestGetWithoutConfig:
    @pytest.mark.asyncio
    async def test_get_raises_when_no_config_supplied(self) -> None:
        registry = VectorStoreRegistry(None, factory=lambda c: MagicMock())
        with pytest.raises(ConfigError, match="no vector store"):
            await registry.get()

    def test_is_configured_false_when_no_config(self) -> None:
        registry = VectorStoreRegistry(None, factory=lambda c: MagicMock())
        assert registry.is_configured is False


class TestGetWithConfig:
    @pytest.mark.asyncio
    async def test_get_constructs_and_caches(self) -> None:
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        store_mock = MagicMock()
        provider_mock.get_vector_store = MagicMock(return_value=store_mock)

        factory = MagicMock(return_value=provider_mock)
        registry = VectorStoreRegistry(_config(), factory=factory)
        assert registry.is_configured

        first = await registry.get()
        second = await registry.get()
        assert first is second
        assert factory.call_count == 1
        provider_mock.initialize.assert_awaited_once()


class TestGetProvider:
    @pytest.mark.asyncio
    async def test_returns_underlying_provider_after_first_get(self) -> None:
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        provider_mock.get_vector_store = MagicMock(return_value=MagicMock())

        registry = VectorStoreRegistry(_config(), factory=lambda c: provider_mock)
        provider = await registry.get_provider()
        assert provider is provider_mock


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_closes_provider(self) -> None:
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        provider_mock.get_vector_store = MagicMock(return_value=MagicMock())

        registry = VectorStoreRegistry(_config(), factory=lambda c: provider_mock)
        await registry.get()
        await registry.invalidate()
        provider_mock.aclose.assert_awaited_once()


class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_closes_provider_when_present(self) -> None:
        provider_mock = MagicMock()
        provider_mock.initialize = AsyncMock()
        provider_mock.aclose = AsyncMock()
        provider_mock.get_vector_store = MagicMock(return_value=MagicMock())

        registry = VectorStoreRegistry(_config(), factory=lambda c: provider_mock)
        await registry.get()
        await registry.aclose()
        provider_mock.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aclose_noop_when_never_initialised(self) -> None:
        registry = VectorStoreRegistry(None, factory=lambda c: MagicMock())
        await registry.aclose()
