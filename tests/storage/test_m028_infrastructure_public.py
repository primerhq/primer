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
    assert any(m.version == 28 and isinstance(m, M028InfrastructurePublic) for m in MIGRATIONS)


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


# -- golden characterization (PERF-T16) ---------------------------------------------------------------------------------------------------

from primer.storage.migrations._rows import read_all  # noqa: E402
from primer.storage.migrations.m028_infrastructure_public import _PUBLIC, _VIEWS, _unset  # noqa: E402
from tests.storage._migration_golden import assert_golden, bulk, insert_raw  # noqa: E402


async def _old_apply(sp) -> None:
    """Migration 28 as it was before PERF-T16, frozen: read each table whole, then ``update`` each unset row."""
    for view in _VIEWS:
        store = sp.get_storage(view)
        for row in await read_all(store):
            if not _unset(row.access):
                continue
            await store.update(row.model_copy(update={"access": dict(_PUBLIC)}))


async def _golden_seed(sp) -> None:
    for n, view in enumerate(_VIEWS):
        await insert_raw(sp, view, {
            f"{view.__name__}-unset": {"provider": "x", "config": {"k": n}},
            f"{view.__name__}-empty": {"access": {}},
            f"{view.__name__}-null": {"access": None},
            f"{view.__name__}-zeroes": {"access": {"owners": [], "grants": {}, "public": False, "rev": 2}},
            f"{view.__name__}-granted": {"access": {"grants": {"u1": "use"}}},
            f"{view.__name__}-owned": {"access": {"owners": ["u1"]}},
            f"{view.__name__}-public": {"access": {"public": True, "members": ["public"]}},
        })
    await insert_raw(sp, _VIEWS[0], bulk("pb", 450, lambda i: {"access": {"owners": ["u1"]}} if i % 3 == 0 else {"n": 1.5}))


async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    await assert_golden(tmp_path, _golden_seed, _VIEWS, _old_apply, M028InfrastructurePublic().apply)
