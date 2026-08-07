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
from primer.model_profile import ResolvedModel, resolve_model
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
