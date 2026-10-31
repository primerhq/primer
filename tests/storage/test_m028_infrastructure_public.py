"""Migration 28: existing profiles and providers become public (access-control Phase 4, Task 3, spec 8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from primer.model.access import Access
from primer.model.model_profile import ModelProfile
from primer.model.provider import SqliteConfig
from primer.model.providers.llm import LLMProvider
from primer.model.workspace import WorkspaceProvider
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m028_infrastructure_public import M028InfrastructurePublic
from primer.storage.sqlite import SqliteStorageProvider


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def test_registered_as_version_28():
    assert MIGRATIONS[-1].version == 28 and isinstance(MIGRATIONS[-1], M028InfrastructurePublic)


async def test_unset_rows_become_public_and_chosen_sharing_is_left(sp):
    profiles = sp.get_storage(ModelProfile)
    await profiles.create(ModelProfile(id="p-old", description="d", provider_id="prov", model_name="m", context_length=1000))
    await profiles.create(
        ModelProfile(id="p-shared", description="d", provider_id="prov", model_name="m", context_length=1000, access=Access(grants={"u1": "read"}, rev=3))
    )
    await sp.get_storage(LLMProvider).create(
        LLMProvider.model_validate({"id": "prov", "provider": "anthropic", "config": {"api_key": "k"}, "limits": {"max_concurrency": 1}})
    )
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider.model_validate({"id": "wp", "provider": "local", "config": {"kind": "local"}})
    )

    await M028InfrastructurePublic().apply(sp)
    await M028InfrastructurePublic().apply(sp)  # converges

    old = await profiles.get("p-old")
    assert old.access.public and "public" in old.access.members
    shared = await profiles.get("p-shared")
    assert shared.access.grants == {"u1": "read"} and not shared.access.public
    assert (await sp.get_storage(LLMProvider).get("prov")).access.public
    assert (await sp.get_storage(WorkspaceProvider).get("wp")).access.public
