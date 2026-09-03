"""Tests for model-profile resolution and override precedence."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.except_ import NotFoundError
from primer.model.model_profile import (
    ModelProfile,
    ModelProfileConfig,
    ReasoningLevel,
)
from primer.model.provider import SqliteConfig
from primer.model_profile import ResolvedModel, resolve_llm, resolve_model
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(
        SqliteConfig(path=str(tmp_path / "profiles.sqlite"))
    )
    await provider.initialize()
    store = provider.get_storage(ModelProfile)
    await store.create(
        ModelProfile(
            id="gx10-qwen-fast",
            description="Qwen, reasoning suppressed.",
            provider_id="gx10",
            model_name="qwen",
            context_length=262144,
            config=ModelProfileConfig(reasoning=ReasoningLevel.OFF),
        )
    )
    await store.create(
        ModelProfile(
            id="gx10-qwen-think",
            description="Qwen, full reasoning.",
            provider_id="gx10",
            model_name="qwen",
            context_length=262144,
            config=ModelProfileConfig(reasoning=ReasoningLevel.HIGH),
        )
    )
    try:
        yield provider
    finally:
        await provider.aclose()


class TestResolveModel:
    async def test_uses_default_when_no_override(self, sp: StorageProvider) -> None:
        r = await resolve_model(sp, default_profile_id="gx10-qwen-fast")
        assert r.profile_id == "gx10-qwen-fast"
        assert r.provider_id == "gx10"
        assert r.model_name == "qwen"
        assert r.context_length == 262144
        assert r.config.reasoning is ReasoningLevel.OFF

    async def test_override_wins(self, sp: StorageProvider) -> None:
        r = await resolve_model(
            sp,
            default_profile_id="gx10-qwen-fast",
            override_profile_id="gx10-qwen-think",
        )
        assert r.profile_id == "gx10-qwen-think"
        assert r.config.reasoning is ReasoningLevel.HIGH

    async def test_none_override_falls_back_to_default(
        self, sp: StorageProvider
    ) -> None:
        r = await resolve_model(
            sp, default_profile_id="gx10-qwen-fast", override_profile_id=None,
        )
        assert r.profile_id == "gx10-qwen-fast"

    async def test_two_profiles_resolve_to_the_same_model(
        self, sp: StorageProvider
    ) -> None:
        """The feature this entity exists for."""
        fast = await resolve_model(sp, default_profile_id="gx10-qwen-fast")
        think = await resolve_model(sp, default_profile_id="gx10-qwen-think")
        assert fast.provider_id == think.provider_id == "gx10"
        assert fast.model_name == think.model_name == "qwen"
        assert fast.config.reasoning is not think.config.reasoning

    async def test_missing_default_raises_not_found(
        self, sp: StorageProvider
    ) -> None:
        with pytest.raises(NotFoundError, match="nope"):
            await resolve_model(sp, default_profile_id="nope")

    async def test_missing_override_raises_rather_than_falling_back(
        self, sp: StorageProvider
    ) -> None:
        """A bad override must fail loudly, not silently run the default."""
        with pytest.raises(NotFoundError, match="ghost"):
            await resolve_model(
                sp,
                default_profile_id="gx10-qwen-fast",
                override_profile_id="ghost",
            )

    async def test_resolved_model_is_frozen(self, sp: StorageProvider) -> None:
        r = await resolve_model(sp, default_profile_id="gx10-qwen-fast")
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.model_name = "other"  # type: ignore[misc]

    async def test_returns_resolved_model_type(self, sp: StorageProvider) -> None:
        r = await resolve_model(sp, default_profile_id="gx10-qwen-fast")
        assert isinstance(r, ResolvedModel)


async def _seed_aggregated(
    sp: StorageProvider, *, id: str, members: list[tuple[str, int]],
) -> None:
    """``members``: list of ``(profile_id, context_length)`` -- seeds one
    leaf ModelProfile per entry plus the aggregated profile naming them,
    IN ORDER.
    """
    store = sp.get_storage(ModelProfile)
    for profile_id, context_length in members:
        await store.create(
            ModelProfile(
                id=profile_id,
                description=f"leaf {profile_id}",
                provider_id=f"prov-{profile_id}",
                model_name=f"model-{profile_id}",
                context_length=context_length,
            )
        )
    await store.create(
        ModelProfile(
            id=id,
            description="an aggregated profile",
            kind="aggregated",
            members=[m[0] for m in members],
        )
    )


class TestResolveModelAggregated:
    """resolve_model's aggregated flat view (no LLM construction)."""

    async def test_provider_id_and_model_name_are_none(
        self, sp: StorageProvider
    ) -> None:
        await _seed_aggregated(
            sp, id="agg-1", members=[("leaf-a", 4096), ("leaf-b", 8192)],
        )
        r = await resolve_model(sp, default_profile_id="agg-1")
        assert r.provider_id is None
        assert r.model_name is None

    async def test_context_length_is_min_over_members(
        self, sp: StorageProvider
    ) -> None:
        await _seed_aggregated(
            sp, id="agg-1", members=[("leaf-a", 200000), ("leaf-b", 4096), ("leaf-c", 32768)],
        )
        r = await resolve_model(sp, default_profile_id="agg-1")
        assert r.context_length == 4096

    async def test_profile_id_is_the_aggregate_own_id(
        self, sp: StorageProvider
    ) -> None:
        await _seed_aggregated(
            sp, id="agg-1", members=[("leaf-a", 4096), ("leaf-b", 8192)],
        )
        r = await resolve_model(sp, default_profile_id="agg-1")
        assert r.profile_id == "agg-1"

    async def test_missing_member_raises_not_found(self, sp: StorageProvider) -> None:
        store = sp.get_storage(ModelProfile)
        await store.create(
            ModelProfile(
                id="agg-broken", description="broken",
                kind="aggregated", members=["ghost"],
            )
        )
        with pytest.raises(NotFoundError, match="ghost"):
            await resolve_model(sp, default_profile_id="agg-broken")

    async def test_nested_aggregate_member_raises_config_error(
        self, sp: StorageProvider
    ) -> None:
        """CRUD-time validation rejects this eagerly on write (ruling 6);
        this pins the resolver's own defense-in-depth for a row that
        somehow got here anyway rather than silently computing a wrong
        MIN.
        """
        from primer.model.except_ import ConfigError

        store = sp.get_storage(ModelProfile)
        await store.create(
            ModelProfile(
                id="inner-agg", description="inner", kind="aggregated",
                members=["leaf-a", "leaf-b"],
            )
        )
        await store.create(
            ModelProfile(
                id="leaf-a", description="leaf", provider_id="p1",
                model_name="m1", context_length=4096,
            )
        )
        await store.create(
            ModelProfile(
                id="leaf-b", description="leaf", provider_id="p2",
                model_name="m2", context_length=8192,
            )
        )
        await store.create(
            ModelProfile(
                id="outer-agg", description="outer", kind="aggregated",
                members=["inner-agg"],
            )
        )
        with pytest.raises(ConfigError, match="nested aggregation"):
            await resolve_model(sp, default_profile_id="outer-agg")


class _FakeProviderRegistry:
    """Minimal get_llm stand-in: returns a distinct sentinel per provider
    id so a test can assert identity without a real adapter."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_llm(self, provider_id: str):
        self.calls.append(provider_id)
        if provider_id is None:
            raise NotFoundError("get_llm called with provider_id=None")
        return f"llm:{provider_id}"


class TestResolveLlmSingle:
    async def test_delegates_to_provider_registry_get_llm(
        self, sp: StorageProvider
    ) -> None:
        registry = _FakeProviderRegistry()
        llm, resolved = await resolve_llm(
            sp, registry, default_profile_id="gx10-qwen-fast",
        )
        assert llm == "llm:gx10"
        assert registry.calls == ["gx10"]
        assert resolved.profile_id == "gx10-qwen-fast"
        assert resolved.provider_id == "gx10"

    async def test_override_wins(self, sp: StorageProvider) -> None:
        registry = _FakeProviderRegistry()
        llm, resolved = await resolve_llm(
            sp, registry,
            default_profile_id="gx10-qwen-fast",
            override_profile_id="gx10-qwen-think",
        )
        assert resolved.profile_id == "gx10-qwen-think"

    async def test_missing_profile_raises_not_found(
        self, sp: StorageProvider
    ) -> None:
        registry = _FakeProviderRegistry()
        with pytest.raises(NotFoundError, match="nope"):
            await resolve_llm(sp, registry, default_profile_id="nope")


class TestResolveLlmAggregated:
    async def test_returns_an_aggregated_llm(self, sp: StorageProvider) -> None:
        from primer.llm.aggregated import AggregatedLLM

        await _seed_aggregated(
            sp, id="agg-1", members=[("leaf-a", 4096), ("leaf-b", 8192)],
        )
        registry = _FakeProviderRegistry()
        llm, resolved = await resolve_llm(sp, registry, default_profile_id="agg-1")
        assert isinstance(llm, AggregatedLLM)
        # Building the aggregated adapter itself does not call get_llm --
        # member resolution is LAZY, deferred to each stream() call (the
        # behaviour AggregatedLLM already had, preserved verbatim).
        assert registry.calls == []
        assert resolved.provider_id is None
        assert resolved.context_length == 4096  # MIN over members

    async def test_resolve_member_recurses_by_profile_id(
        self, sp: StorageProvider
    ) -> None:
        """The member-resolution callable AggregatedLLM is built with
        recurses through resolve_llm itself, keyed by member PROFILE id
        -- confirmed end to end by resolving a member and checking it
        reaches the SAME provider_registry.get_llm the top-level single
        path uses.
        """
        await _seed_aggregated(
            sp, id="agg-1", members=[("leaf-a", 4096), ("leaf-b", 8192)],
        )
        registry = _FakeProviderRegistry()
        llm, _resolved = await resolve_llm(sp, registry, default_profile_id="agg-1")
        member_llm, member_resolved = await llm._resolve("leaf-a")
        assert member_llm == "llm:prov-leaf-a"
        assert member_resolved.model_name == "model-leaf-a"
        assert registry.calls == ["prov-leaf-a"]
