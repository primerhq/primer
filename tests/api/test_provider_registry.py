"""Unit tests for primer.api.registries.provider_registry.ProviderRegistry."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from primer.api.registries.provider_registry import ProviderRegistry
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.model_profile import ModelProfile, RoutingStrategy
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

    async def update_unless(
        self,
        entity,
        *,
        field,
        forbidden,
        conn=None,
    ):
        current = self._data.get(entity.id)
        if current is None:
            raise NotFoundError(f"no entity with id {entity.id!r}")
        if getattr(current, field, None) == forbidden:
            return None
        self._data[entity.id] = entity
        return entity

    async def delete(self, id: str) -> None:
        self._data.pop(id, None)


class _FakeStorageProvider:
    async def get_system_state(self):
        from primer.model.system_state import SystemState

        return SystemState()

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


def _make_aggregated_profile(
    *, strategy: RoutingStrategy = RoutingStrategy.SEQUENTIAL,
) -> ModelProfile:
    return ModelProfile(
        id="agg-1",
        description="aggregated profile",
        kind="aggregated",
        members=["m1", "m2"],
        strategy=strategy,
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


class TestAggregatedLLMResolution:
    """AggregatedLLM must be cached per profile id, not rebuilt per call --
    otherwise its round-robin _cursor resets every time and ROUND_ROBIN
    routing never actually rotates past member[0]."""

    @staticmethod
    async def _resolve_member(member_id: str) -> tuple[Any, Any]:
        return MagicMock(), MagicMock()

    @pytest.mark.asyncio
    async def test_lookup_constructs_and_caches(self) -> None:
        sp = _FakeStorageProvider()
        profile = _make_aggregated_profile()
        await sp.get_storage(ModelProfile).create(profile)
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())

        first = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )
        second = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )
        assert first is second

    @pytest.mark.asyncio
    async def test_missing_profile_raises_not_found(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())
        with pytest.raises(NotFoundError, match="agg-1"):
            await registry.get_aggregated_llm(
                "agg-1", resolve_member=self._resolve_member,
            )

    @pytest.mark.asyncio
    async def test_round_robin_cursor_persists_across_calls(self) -> None:
        """The regression this cache exists to fix: a fresh AggregatedLLM
        built on every call would reset _cursor to 0 every time, so
        ROUND_ROBIN's start position would never advance past member[0]."""
        sp = _FakeStorageProvider()
        profile = _make_aggregated_profile(strategy=RoutingStrategy.ROUND_ROBIN)
        await sp.get_storage(ModelProfile).create(profile)
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())

        llm1 = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )
        order1 = await llm1._member_order()
        llm2 = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )
        order2 = await llm2._member_order()

        assert llm1 is llm2
        assert order1 != order2, (
            "cursor did not advance across calls - round-robin would "
            "always start at the same member"
        )

    @pytest.mark.asyncio
    async def test_invalidate_drops_cache(self) -> None:
        sp = _FakeStorageProvider()
        profile = _make_aggregated_profile()
        await sp.get_storage(ModelProfile).create(profile)
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())

        first = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )
        await registry.invalidate_aggregated_llm(profile.id)
        second = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )
        assert first is not second

    @pytest.mark.asyncio
    async def test_invalidate_unknown_id_is_noop(self) -> None:
        sp = _FakeStorageProvider()
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())
        await registry.invalidate_aggregated_llm("never-cached")

    @pytest.mark.asyncio
    async def test_flush_mid_lookup_is_not_resurrected(self) -> None:
        """01a067c4 gate MAJOR: get_aggregated_llm used to take an
        already-fetched row instead of re-fetching inside its own lock,
        so its _cache_generation sample was vacuous - zero awaits stood
        between the sample and the cache insert, and the real race span
        (the CALLER's row fetch, entirely outside this method and its
        lock) was uncovered. A stale flush/invalidation landing there
        would leave a permanently stale AggregatedLLM (old
        members/strategy) cached forever.

        Single-key invalidate_aggregated_llm can't actually race this
        method at all post-fix: both it and get_aggregated_llm hold the
        SAME asyncio.Lock for their entire bodies, including the row
        fetch, so they're mutually exclusive by construction (same as
        get_llm/invalidate_llm already were). The genuine race is
        against _flush_caches_local - the ONE lock-free path (it must
        run synchronously from the bus-reconnect hook, per its own
        docstring) - landing while THIS call's in-lock row fetch is
        still in flight. This pins that: the generation-guard must
        refuse to cache the adapter built from what is now a stale
        generation.
        """
        sp = _FakeStorageProvider()
        profile = _make_aggregated_profile()
        await sp.get_storage(ModelProfile).create(profile)
        registry = ProviderRegistry(sp, llm_factory=lambda p: MagicMock())

        storage = sp.get_storage(ModelProfile)
        real_get = storage.get

        async def _get_then_flush(id: str):
            row = await real_get(id)
            # Simulate _flush_caches_local landing while THIS call's own
            # row fetch (inside the lock) is still in flight - the exact
            # window the generation-guard exists to cover.
            registry._flush_caches_local()
            return row

        storage.get = _get_then_flush  # type: ignore[method-assign]

        adapter = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )

        assert profile.id not in registry._aggregated_llm_cache, (
            "an adapter built during an in-flight flush must not be "
            "cached - it would stay stale forever"
        )
        # The call itself must still succeed and return a real adapter --
        # only the CACHING is refused, not the lookup.
        from primer.llm.aggregated import AggregatedLLM
        assert isinstance(adapter, AggregatedLLM)

        # A subsequent call rebuilds (cache miss) rather than returning
        # the never-cached, now-stale adapter from above.
        storage.get = real_get
        second = await registry.get_aggregated_llm(
            profile.id, resolve_member=self._resolve_member,
        )
        assert second is not adapter
        assert profile.id in registry._aggregated_llm_cache


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
        await sp.get_storage(CrossEncoderProvider).create(
            _make_cross_encoder_provider()
        )

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
        await sp.get_storage(CrossEncoderProvider).create(
            _make_cross_encoder_provider()
        )

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
