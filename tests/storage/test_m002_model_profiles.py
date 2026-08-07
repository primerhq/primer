"""Tests for the models[] -> ModelProfile cutover migration."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from primer.int.storage_provider import StorageProvider
from primer.model.model_profile import ModelProfile
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.storage.migrations.m002_model_profiles import (
    Agent as LegacyAgent,
    LLMProvider as LegacyProvider,
    M002ModelProfiles,
    synth_profile_id,
)
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "m002.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _seed_provider(sp: StorageProvider, **extra: object) -> None:
    await sp.get_storage(LegacyProvider).create(
        LegacyProvider(
            id="gx10",
            models=[
                {"name": "qwen", "context_length": 262144},
                {"name": "Qwen/Qwen3-32B", "context_length": 32768},
            ],
            **extra,  # type: ignore[arg-type]
        )
    )


async def _seed_agent(sp: StorageProvider, model: dict[str, object]) -> None:
    await sp.get_storage(LegacyAgent).create(
        LegacyAgent(id="reviewer", model=model, description="A reviewer.")  # type: ignore[call-arg]
    )


async def _all_profiles(sp: StorageProvider) -> list[ModelProfile]:
    page = await sp.get_storage(ModelProfile).list(OffsetPage(offset=0, length=200))
    return list(page.items)


class TestSynthProfileId:
    def test_simple(self) -> None:
        assert synth_profile_id("gx10", "qwen") == "gx10--qwen"

    def test_sanitises_slashes_and_case(self) -> None:
        assert synth_profile_id("gx10", "Qwen/Qwen3-32B") == "gx10--qwen-qwen3-32b"

    def test_strips_leading_and_trailing_separators(self) -> None:
        assert synth_profile_id("p", "/weird/") == "p--weird"

    def test_is_deterministic(self) -> None:
        assert synth_profile_id("a", "b") == synth_profile_id("a", "b")


class TestSynthesiseProfiles:
    async def test_creates_one_profile_per_model_entry(
        self, sp: StorageProvider
    ) -> None:
        await _seed_provider(sp)
        await M002ModelProfiles().apply(sp)

        profiles = await _all_profiles(sp)
        assert {p.id for p in profiles} == {"gx10--qwen", "gx10--qwen-qwen3-32b"}

    async def test_carries_context_length_across(self, sp: StorageProvider) -> None:
        await _seed_provider(sp)
        await M002ModelProfiles().apply(sp)

        qwen = next(p for p in await _all_profiles(sp) if p.id == "gx10--qwen")
        assert qwen.context_length == 262144
        assert qwen.provider_id == "gx10"
        assert qwen.model_name == "qwen"

    async def test_migrated_profile_config_is_inert(
        self, sp: StorageProvider
    ) -> None:
        """A migrated profile must behave exactly as the old row did."""
        await _seed_provider(sp)
        await M002ModelProfiles().apply(sp)

        qwen = next(p for p in await _all_profiles(sp) if p.id == "gx10--qwen")
        assert qwen.config.reasoning is None
        assert qwen.config.extended == {}


class TestRewriteAgents:
    async def test_rewrites_model_block_to_profile_id(
        self, sp: StorageProvider
    ) -> None:
        await _seed_provider(sp)
        await _seed_agent(sp, {"provider_id": "gx10", "model_name": "qwen"})

        await M002ModelProfiles().apply(sp)

        row = await sp.get_storage(LegacyAgent).get("reviewer")
        assert row is not None
        assert row.model == {"profile_id": "gx10--qwen"}

    async def test_preserves_other_agent_fields(self, sp: StorageProvider) -> None:
        await _seed_provider(sp)
        await _seed_agent(sp, {"provider_id": "gx10", "model_name": "qwen"})

        await M002ModelProfiles().apply(sp)

        row = await sp.get_storage(LegacyAgent).get("reviewer")
        assert row is not None
        assert (row.model_extra or {}).get("description") == "A reviewer."

    async def test_already_migrated_agent_is_left_alone(
        self, sp: StorageProvider
    ) -> None:
        await _seed_provider(sp)
        await _seed_agent(sp, {"profile_id": "hand-picked"})

        await M002ModelProfiles().apply(sp)

        row = await sp.get_storage(LegacyAgent).get("reviewer")
        assert row is not None
        assert row.model == {"profile_id": "hand-picked"}

    async def test_orphan_agent_is_logged_not_rewritten(
        self, sp: StorageProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An agent naming a provider that no longer exists was already broken."""
        await _seed_agent(sp, {"provider_id": "ghost", "model_name": "qwen"})

        with caplog.at_level(logging.WARNING):
            await M002ModelProfiles().apply(sp)

        row = await sp.get_storage(LegacyAgent).get("reviewer")
        assert row is not None
        assert row.model == {"provider_id": "ghost", "model_name": "qwen"}
        assert any(
            "no matching provider" in r.getMessage() for r in caplog.records
        )


class TestStripModels:
    async def test_removes_models_from_provider(self, sp: StorageProvider) -> None:
        await _seed_provider(sp)
        await M002ModelProfiles().apply(sp)

        row = await sp.get_storage(LegacyProvider).get("gx10")
        assert row is not None
        assert row.models is None

    async def test_preserves_other_provider_fields(
        self, sp: StorageProvider
    ) -> None:
        await _seed_provider(sp, provider="openresponses")
        await M002ModelProfiles().apply(sp)

        row = await sp.get_storage(LegacyProvider).get("gx10")
        assert row is not None
        assert (row.model_extra or {}).get("provider") == "openresponses"


class TestIdempotency:
    async def test_second_run_is_a_noop(self, sp: StorageProvider) -> None:
        await _seed_provider(sp)
        await _seed_agent(sp, {"provider_id": "gx10", "model_name": "qwen"})

        await M002ModelProfiles().apply(sp)
        first = {p.id for p in await _all_profiles(sp)}
        agent_first = (await sp.get_storage(LegacyAgent).get("reviewer")).model

        await M002ModelProfiles().apply(sp)
        second = {p.id for p in await _all_profiles(sp)}
        agent_second = (await sp.get_storage(LegacyAgent).get("reviewer")).model

        assert first == second
        assert agent_first == agent_second

    async def test_empty_database_is_a_noop(self, sp: StorageProvider) -> None:
        await M002ModelProfiles().apply(sp)
        assert await _all_profiles(sp) == []
