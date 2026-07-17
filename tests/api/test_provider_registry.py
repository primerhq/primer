"""Unit tests for primer.api.registries.provider_registry.ProviderRegistry."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from primer.api.registries.provider_registry import ProviderRegistry
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.provider import (
    AnthropicConfig,
    CrossEncoderModel,
    CrossEncoderProvider,
    CrossEncoderProviderType,
    EmbeddingModel,
    EmbeddingProvider,
    EmbeddingProviderType,
    HuggingFaceConfig,
    HuggingFaceCrossEncoderConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


class _FakeStorage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id: str):
        return self._data.get(id)

    async def create(self, entity):
        self._data[entity.id] = entity
        return entity

    async def update(self, entity):
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._data.pop(id, None)


class _FakeStorageProvider:
    def __init__(self) -> None:
        self._stores: dict[type, _FakeStorage] = {}

    def get_storage(self, model_class: type) -> _FakeStorage:
        return self._stores.setdefault(model_class, _FakeStorage())

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


def _make_llm_provider() -> LLMProvider:
    return LLMProvider(
        id="anthropic-1",
        provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="claude-sonnet-4-6", context_length=200_000)],
        config=AnthropicConfig(api_key=SecretStr("sk-x")),
        limits=Limits(max_concurrency=4),
    )


def _make_embedding_provider() -> EmbeddingProvider:
    return EmbeddingProvider(
        id="hf-1",
        provider=EmbeddingProviderType.HUGGINGFACE,
        models=[EmbeddingModel(name="sentence-transformers/all-MiniLM-L6-v2")],
        config=HuggingFaceConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=2),
    )


def _make_cross_encoder_provider() -> CrossEncoderProvider:
    return CrossEncoderProvider(
        id="ce-1",
        provider=CrossEncoderProviderType.HUGGINGFACE,
        models=[CrossEncoderModel(name="BAAI/bge-reranker-v2-m3")],
        config=HuggingFaceCrossEncoderConfig(token=SecretStr("hf_x")),
        limits=Limits(max_concurrency=2),
    )


class TestLLMResolution:
    @pytest.mark.asyncio
    async def test_lookup_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())

        ctor = MagicMock(return_value=MagicMock())
        registry = ProviderRegistry(sp, llm_factory=lambda p: ctor(p))

        first = await registry.get_llm("anthropic-1")
        second = await registry.get_llm("anthropic-1")
        assert first is second
        assert ctor.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_provider_raises_not_found(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())
        with pytest.raises(NotFoundError, match="anthropic-1"):
            await registry.get_llm("anthropic-1")

    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_calls_aclose(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())

        adapter = MagicMock()
        adapter.aclose = AsyncMock()
        ctor = MagicMock(return_value=adapter)
        registry = ProviderRegistry(sp, llm_factory=lambda p: ctor(p))

        await registry.get_llm("anthropic-1")
        await registry.invalidate_llm("anthropic-1")

        adapter.aclose.assert_awaited_once()
        await registry.get_llm("anthropic-1")
        assert ctor.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_unknown_id_is_noop(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())
        await registry.invalidate_llm("never-cached")


class TestEmbedderResolution:
    @pytest.mark.asyncio
    async def test_lookup_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(EmbeddingProvider).create(_make_embedding_provider())

        ctor = MagicMock(return_value=MagicMock())
        registry = ProviderRegistry(sp, embedder_factory=lambda p: ctor(p))

        first = await registry.get_embedder("hf-1")
        second = await registry.get_embedder("hf-1")
        assert first is second
        assert ctor.call_count == 1


class TestToolsetDispatchDefault:
    @pytest.mark.asyncio
    async def test_default_factory_constructs_mcp_provider(self) -> None:
        from primer.model.provider import (
            McpConfig,
            StdioConfig,
            Toolset,
            ToolsetProviderType,
            TransportType,
        )
        from primer.toolset.mcp import McpToolsetProvider

        sp = _FakeStorageProvider()
        await sp.get_storage(Toolset).create(
            Toolset(
                id="t1",
                provider=ToolsetProviderType.MCP,
                config=McpConfig(
                    transport=TransportType.STDIO,
                    config=StdioConfig(command=["x"]),
                ),
            )
        )
        registry = ProviderRegistry(sp)
        provider = await registry.get_toolset("t1")
        assert isinstance(provider, McpToolsetProvider)


class TestCrossEncoderResolution:
    @pytest.mark.asyncio
    async def test_lookup_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(CrossEncoderProvider).create(_make_cross_encoder_provider())

        ctor = MagicMock(return_value=MagicMock())
        registry = ProviderRegistry(sp, cross_encoder_factory=lambda p: ctor(p))

        first = await registry.get_cross_encoder("ce-1")
        second = await registry.get_cross_encoder("ce-1")
        assert first is second
        assert ctor.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_provider_raises_not_found(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, cross_encoder_factory=lambda p: MagicMock())
        with pytest.raises(NotFoundError, match="ce-missing"):
            await registry.get_cross_encoder("ce-missing")

    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_calls_aclose(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(CrossEncoderProvider).create(_make_cross_encoder_provider())

        adapter = MagicMock()
        adapter.aclose = AsyncMock()
        registry = ProviderRegistry(sp, cross_encoder_factory=lambda p: adapter)

        await registry.get_cross_encoder("ce-1")
        await registry.invalidate_cross_encoder("ce-1")
        adapter.aclose.assert_awaited_once()


class TestEmbedderInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_drops_cache_and_calls_aclose(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(EmbeddingProvider).create(_make_embedding_provider())

        adapter = MagicMock()
        adapter.aclose = AsyncMock()
        registry = ProviderRegistry(sp, embedder_factory=lambda p: adapter)

        await registry.get_embedder("hf-1")
        await registry.invalidate_embedder("hf-1")
        adapter.aclose.assert_awaited_once()


class TestToolsetInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_unknown_id_is_noop(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, toolset_factory=lambda t: MagicMock())
        await registry.invalidate_toolset("never-cached")


class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_calls_each_cached_adapter_and_clears(self) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())
        await sp.get_storage(EmbeddingProvider).create(_make_embedding_provider())

        llm_adapter = MagicMock()
        llm_adapter.aclose = AsyncMock()
        emb_adapter = MagicMock()
        emb_adapter.aclose = AsyncMock()

        registry = ProviderRegistry(
            sp,
            llm_factory=lambda p: llm_adapter,
            embedder_factory=lambda p: emb_adapter,
        )
        await registry.get_llm("anthropic-1")
        await registry.get_embedder("hf-1")

        await registry.aclose()
        llm_adapter.aclose.assert_awaited_once()
        emb_adapter.aclose.assert_awaited_once()


class TestFlushDuringInFlightGet:
    """A reconnect flush must not be undone by an in-flight ``get_*``.

    ``get_*`` holds ``registry._lock`` across the storage await, but
    ``_flush_caches_local`` (the subscription's ``on_reconnect`` hook) takes
    no lock, so it lands while a get is suspended. The get then resumes and
    inserts an adapter built from a PRE-flush row, re-caching the stale
    adapter (e.g. a rotated API key) until restart -- the exact bug the
    flush exists to fix. A generation counter makes the in-flight get skip
    its cache insert (see arch-review batch 1, MEDIUM-2).
    """

    @pytest.mark.asyncio
    async def test_flush_during_suspended_get_does_not_recache_stale(
        self,
    ) -> None:
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())

        stale = MagicMock(name="stale")
        stale.aclose = AsyncMock()
        fresh = MagicMock(name="fresh")
        fresh.aclose = AsyncMock()
        built: list[Any] = []

        def _factory(row: LLMProvider):
            adapter = stale if not built else fresh
            built.append(adapter)
            return adapter

        registry = ProviderRegistry(sp, llm_factory=_factory)

        storage = sp.get_storage(LLMProvider)
        real_get = storage.get
        suspended = asyncio.Event()
        release = asyncio.Event()

        async def _hooked_get(id: str):
            row = await real_get(id)
            # Suspend INSIDE get_llm's storage await, holding registry._lock.
            suspended.set()
            await release.wait()
            return row

        storage.get = _hooked_get  # type: ignore[assignment]
        task = asyncio.create_task(registry.get_llm("anthropic-1"))
        await suspended.wait()

        # The subscription's reconnect hook fires while the get is suspended.
        registry._flush_caches_local()

        release.set()
        adapter = await task

        # The in-flight caller still gets a usable adapter back: its request
        # must complete, not fail, just because a flush raced it.
        assert adapter is stale
        # ...but the pre-flush adapter must NOT be left in the cache.
        assert registry._llm_cache == {}

        # The next get rebuilds from a post-flush row and caches normally.
        storage.get = real_get  # type: ignore[assignment]
        assert await registry.get_llm("anthropic-1") is fresh
        assert registry._llm_cache["anthropic-1"] is fresh

    @pytest.mark.asyncio
    async def test_get_without_racing_flush_still_caches(self) -> None:
        """The generation guard must not break the ordinary caching path."""
        sp = _FakeStorageProvider()
        await sp.get_storage(LLMProvider).create(_make_llm_provider())
        adapter = MagicMock()
        adapter.aclose = AsyncMock()
        registry = ProviderRegistry(sp, llm_factory=lambda p: adapter)

        first = await registry.get_llm("anthropic-1")
        assert registry._llm_cache["anthropic-1"] is adapter
        # Second get is served from cache (no rebuild).
        assert await registry.get_llm("anthropic-1") is first
