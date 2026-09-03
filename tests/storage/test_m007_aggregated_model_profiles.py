"""Tests for the aggregated-LLMProvider -> aggregated-ModelProfile cutover
migration."""

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
from primer.storage.migrations.m002_model_profiles import synth_profile_id
from primer.storage.migrations.m007_aggregated_model_profiles import (
    LLMProvider as LegacyProvider,
    M007AggregatedModelProfiles,
)
from primer.storage.sqlite import SqliteStorageProvider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[StorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "m007.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def _seed_leaf_profile(
    sp: StorageProvider, *, provider_id: str, model_name: str, context_length: int,
) -> ModelProfile:
    profile = ModelProfile(
        id=synth_profile_id(provider_id, model_name),
        description=f"leaf profile for {provider_id}/{model_name}",
        provider_id=provider_id,
        model_name=model_name,
        context_length=context_length,
    )
    await sp.get_storage(ModelProfile).create(profile)
    return profile


async def _seed_aggregated_provider(
    sp: StorageProvider, *, id: str, members: list[dict], **cfg_extra: object,
) -> None:
    await sp.get_storage(LegacyProvider).create(
        LegacyProvider(
            id=id,
            provider="aggregated",
            config={"members": members, **cfg_extra},
        )
    )


async def _seed_virtual_profile(
    sp: StorageProvider, *, id: str, aggregated_provider_id: str, virtual_name: str,
) -> None:
    """A pre-migration profile pointing at an aggregated LLMProvider --
    the "virtual name" shape ``tests/api/test_aggregated_llm_provider.py``
    exercises today.
    """
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id=id,
            description="a pre-migration virtual profile",
            provider_id=aggregated_provider_id,
            model_name=virtual_name,
            context_length=8192,
        )
    )


class TestConvertProfiles:
    async def test_converts_profile_to_aggregated_kind(
        self, sp: StorageProvider
    ) -> None:
        await _seed_leaf_profile(
            sp, provider_id="p1", model_name="m1", context_length=4096,
        )
        await _seed_leaf_profile(
            sp, provider_id="p2", model_name="m2", context_length=8192,
        )
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)

        row = await sp.get_storage(ModelProfile).get("virtual-1")
        assert row is not None
        assert row.kind == "aggregated"
        assert row.provider_id is None
        assert row.model_name is None
        assert row.context_length is None

    async def test_id_is_preserved_so_references_do_not_rewrite(
        self, sp: StorageProvider
    ) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)

        # Same id before and after -- an Agent.model.profile_id or a graph
        # node's profile_id override naming "virtual-1" keeps working
        # without any rewrite.
        assert await sp.get_storage(ModelProfile).get("virtual-1") is not None

    async def test_members_preserve_order(self, sp: StorageProvider) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_leaf_profile(sp, provider_id="p3", model_name="m3", context_length=16384)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p3", "model_name": "m3"},
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)

        row = await sp.get_storage(ModelProfile).get("virtual-1")
        assert row is not None
        assert row.members == [
            synth_profile_id("p3", "m3"),
            synth_profile_id("p1", "m1"),
            synth_profile_id("p2", "m2"),
        ]

    async def test_carries_routing_policy_across(self, sp: StorageProvider) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
            strategy="round_robin",
            failover_point="mid_stream",
            failover_on="transient",
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)

        row = await sp.get_storage(ModelProfile).get("virtual-1")
        assert row is not None
        assert row.strategy == "round_robin"
        assert row.failover_point == "mid_stream"
        assert row.failover_on == "transient"

    async def test_defaults_routing_policy_when_config_omits_it(
        self, sp: StorageProvider
    ) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)

        row = await sp.get_storage(ModelProfile).get("virtual-1")
        assert row is not None
        assert row.strategy == "sequential"
        assert row.failover_point == "before_first_token"
        assert row.failover_on == "transient_and_config"

    async def test_reuses_existing_member_profile(self, sp: StorageProvider) -> None:
        await _seed_leaf_profile(
            sp, provider_id="p1", model_name="m1", context_length=999999,
        )
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)

        member = await sp.get_storage(ModelProfile).get(synth_profile_id("p1", "m1"))
        assert member is not None
        assert member.context_length == 999999  # untouched, not overwritten

    async def test_synthesises_missing_member_profile(
        self, sp: StorageProvider, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A member naming a (provider_id, model_name) m002 never saw a
        provider for -- no pre-existing leaf profile. Synthesised with the
        documented fallback context_length, loudly logged."""
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "ghost", "model_name": "phantom"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        with caplog.at_level(logging.WARNING):
            await M007AggregatedModelProfiles().apply(sp)

        synthesised = await sp.get_storage(ModelProfile).get(
            synth_profile_id("ghost", "phantom")
        )
        assert synthesised is not None
        assert synthesised.kind == "single"
        assert synthesised.context_length == 8192  # documented fallback
        assert any(
            "synthesised a leaf profile" in r.getMessage() for r in caplog.records
        )

    async def test_malformed_member_is_skipped_and_logged(
        self, sp: StorageProvider, caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2"},  # missing model_name
                {"provider_id": "p3", "model_name": "m3"},
            ],
        )
        # Two RESOLVABLE members (p1/m1, p3/m3 synthesised) -- >= 2, so
        # this converts; the malformed entry is dropped, not counted.
        await _seed_leaf_profile(sp, provider_id="p3", model_name="m3", context_length=2048)
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        with caplog.at_level(logging.WARNING):
            await M007AggregatedModelProfiles().apply(sp)

        row = await sp.get_storage(ModelProfile).get("virtual-1")
        assert row is not None
        assert row.kind == "aggregated"
        assert row.members == [
            synth_profile_id("p1", "m1"),
            synth_profile_id("p3", "m3"),
        ]
        assert any(
            "malformed member" in r.getMessage() for r in caplog.records
        )

    async def test_already_aggregated_profile_is_left_alone(
        self, sp: StorageProvider
    ) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await sp.get_storage(ModelProfile).create(
            ModelProfile(
                id="already-aggregated",
                description="already converted",
                kind="aggregated",
                members=[
                    synth_profile_id("p1", "m1"), synth_profile_id("p2", "m2"),
                ],
            )
        )

        await M007AggregatedModelProfiles().apply(sp)

        row = await sp.get_storage(ModelProfile).get("already-aggregated")
        assert row is not None
        assert row.members == [
            synth_profile_id("p1", "m1"), synth_profile_id("p2", "m2"),
        ]

    async def test_fewer_than_two_resolvable_members_leaves_profile_unconverted(
        self, sp: StorageProvider, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The OLD schema allowed a 1-member aggregation (min_length=1);
        the new shape requires >= 2. Nothing to grandfather (irreproducible
        via any current write path) -- this pins the defensive behaviour:
        leave the profile pointing at a provider id that step 2 deletes,
        loudly logged rather than silently invented.
        """
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_aggregated_provider(
            sp, id="agg-1", members=[{"provider_id": "p1", "model_name": "m1"}],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        with caplog.at_level(logging.WARNING):
            await M007AggregatedModelProfiles().apply(sp)

        row = await sp.get_storage(ModelProfile).get("virtual-1")
        assert row is not None
        assert row.kind == "single"  # unconverted
        assert row.provider_id == "agg-1"  # now dangling -- see next test
        assert any(
            "UNCONVERTED" in r.getMessage() for r in caplog.records
        )


class TestDeleteOrphanedProviders:
    async def test_deletes_aggregated_provider(self, sp: StorageProvider) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)

        assert await sp.get_storage(LegacyProvider).get("agg-1") is None

    async def test_preserves_non_aggregated_providers(
        self, sp: StorageProvider
    ) -> None:
        await sp.get_storage(LegacyProvider).create(
            LegacyProvider(id="anthropic-1", provider="anthropic", config={}),
        )

        await M007AggregatedModelProfiles().apply(sp)

        assert await sp.get_storage(LegacyProvider).get("anthropic-1") is not None

    async def test_deletes_orphan_even_when_no_profile_pointed_at_it(
        self, sp: StorageProvider
    ) -> None:
        """An aggregated provider with zero ModelProfile rows naming it --
        after this migration the row is unreadable by the live model
        regardless, so it must go too, not just providers a profile
        happened to reference."""
        await _seed_aggregated_provider(
            sp, id="agg-orphan", members=[{"provider_id": "p1", "model_name": "m1"}],
        )

        await M007AggregatedModelProfiles().apply(sp)

        assert await sp.get_storage(LegacyProvider).get("agg-orphan") is None

    async def test_deleting_unconverted_orphan_logs_loudly(
        self, sp: StorageProvider, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Lead-requested addition: when step 2 deletes a provider whose
        profile could NOT be converted (the defensive < 2 members case),
        that must be an ERROR-level, count+ids log line -- not just the
        earlier step-1 WARNING -- since this is the point real data
        actually ends up dangling.
        """
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_aggregated_provider(
            sp, id="agg-1", members=[{"provider_id": "p1", "model_name": "m1"}],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        with caplog.at_level(logging.WARNING):
            await M007AggregatedModelProfiles().apply(sp)

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "expected an ERROR-level log for the deleted unconverted orphan"
        assert any("agg-1" in str(r.__dict__.get("provider_ids", [])) for r in errors)


class TestIdempotency:
    async def test_second_run_is_a_noop(self, sp: StorageProvider) -> None:
        await _seed_leaf_profile(sp, provider_id="p1", model_name="m1", context_length=4096)
        await _seed_leaf_profile(sp, provider_id="p2", model_name="m2", context_length=8192)
        await _seed_aggregated_provider(
            sp, id="agg-1",
            members=[
                {"provider_id": "p1", "model_name": "m1"},
                {"provider_id": "p2", "model_name": "m2"},
            ],
        )
        await _seed_virtual_profile(
            sp, id="virtual-1", aggregated_provider_id="agg-1", virtual_name="pool",
        )

        await M007AggregatedModelProfiles().apply(sp)
        first = (await sp.get_storage(ModelProfile).get("virtual-1")).members

        await M007AggregatedModelProfiles().apply(sp)
        second = (await sp.get_storage(ModelProfile).get("virtual-1")).members

        assert first == second

    async def test_empty_database_is_a_noop(self, sp: StorageProvider) -> None:
        await M007AggregatedModelProfiles().apply(sp)
        page = await sp.get_storage(ModelProfile).list(OffsetPage(offset=0, length=200))
        assert page.items == []
