"""``resolve_default_workspace_id`` (pivot P2 Task 1): the one reader of the default workspace.

The recorded ``system_state.default_workspace_id`` wins. While it is unset, the legacy ``primer`` row (what an
install created before the field existed has) is the default; with neither, there is no default yet. Nothing
else in the code base may hard-code the id, so this is what the delete guard, ``is_default`` and the console read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest_asyncio

from primer.bootstrap.defaults import (
    LEGACY_DEFAULT_WORKSPACE_ID,
    PRIME_WORKSPACE_ID,
    PRIME_WORKSPACE_NAME,
)
from primer.bootstrap.prime import (
    adopt_legacy_default_workspace,
    record_default_workspace_id,
    resolve_default_workspace_id,
)
from primer.model.provider import SqliteConfig
from primer.model.workspace import (
    Workspace,
    WorkspaceChannelLink,
    WorkspaceRuntimeMeta,
    WorkspaceTemplateOverrides,
)
from primer.storage.sqlite import SqliteStorageProvider


def _row(workspace_id: str) -> Workspace:
    """A durable workspace row, shaped as ``ensure_default_workspace`` writes it."""
    return Workspace(
        id=workspace_id,
        name=workspace_id,
        template_id="local-default",
        provider_id="local",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token"),
    )


def test_the_constants_name_the_two_ids():
    assert LEGACY_DEFAULT_WORKSPACE_ID == "primer"
    assert PRIME_WORKSPACE_ID == "prime"
    assert PRIME_WORKSPACE_NAME == "prime"


async def test_the_recorded_id_wins(fake_storage_provider):
    await fake_storage_provider.set_default_workspace_id("prime")
    assert await resolve_default_workspace_id(fake_storage_provider) == "prime"


async def test_the_recorded_id_wins_even_when_a_legacy_row_exists(fake_storage_provider):
    """An install that adopted ``primer`` and one that recorded something else must agree on ONE default."""
    await fake_storage_provider.get_storage(Workspace).create(_row("primer"))
    await fake_storage_provider.set_default_workspace_id("prime")
    assert await resolve_default_workspace_id(fake_storage_provider) == "prime"


async def test_a_recorded_id_is_returned_even_if_its_row_is_missing(fake_storage_provider):
    """The resolver names the default; whether its row still exists is the ensure pass's business (it re-creates a missing one)."""
    await fake_storage_provider.set_default_workspace_id("prime")
    assert await fake_storage_provider.get_storage(Workspace).get("prime") is None
    assert await resolve_default_workspace_id(fake_storage_provider) == "prime"


async def test_an_unrecorded_install_falls_back_to_a_legacy_row(fake_storage_provider):
    await fake_storage_provider.get_storage(Workspace).create(_row("primer"))
    assert await resolve_default_workspace_id(fake_storage_provider) == "primer"


async def test_no_record_and_no_legacy_row_is_none(fake_storage_provider):
    assert await resolve_default_workspace_id(fake_storage_provider) is None


async def test_another_workspace_is_never_taken_for_the_default(fake_storage_provider):
    """Only the legacy id is a fallback: an unrelated row (even one called ``prime``) is not the default until it is recorded."""
    await fake_storage_provider.get_storage(Workspace).create(_row("ws-1"))
    await fake_storage_provider.get_storage(Workspace).create(_row("prime"))
    assert await resolve_default_workspace_id(fake_storage_provider) is None


async def test_clearing_the_record_returns_to_the_fallback(fake_storage_provider):
    await fake_storage_provider.get_storage(Workspace).create(_row("primer"))
    await fake_storage_provider.set_default_workspace_id("prime")
    await fake_storage_provider.set_default_workspace_id(None)
    assert await resolve_default_workspace_id(fake_storage_provider) == "primer"


# ---- adopting an install that predates the record (task 4) ----------------------------------------------------------------------


async def test_adoption_records_the_legacy_row_and_renames_it_for_display(fake_storage_provider):
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer"))
    assert (await ws_store.get("primer")).name == "primer"
    assert await adopt_legacy_default_workspace(fake_storage_provider) == "primer"
    assert (await fake_storage_provider.get_system_state()).default_workspace_id == "primer"
    assert (await ws_store.get("primer")).name == "prime"
    assert await resolve_default_workspace_id(fake_storage_provider) == "primer", "the id is kept; only the display name moves"


async def test_adoption_names_an_unnamed_legacy_row_too(fake_storage_provider):
    ws_store = fake_storage_provider.get_storage(Workspace)
    row = _row("primer")
    await ws_store.create(row.model_copy(update={"name": None}))
    assert await adopt_legacy_default_workspace(fake_storage_provider) == "primer"
    assert (await ws_store.get("primer")).name == "prime"


async def test_adoption_without_a_legacy_row_records_nothing(fake_storage_provider):
    await fake_storage_provider.get_storage(Workspace).create(_row("prime"))
    assert await adopt_legacy_default_workspace(fake_storage_provider) is None
    assert (await fake_storage_provider.get_system_state()).default_workspace_id is None


async def test_adoption_runs_once_and_a_later_rename_survives(fake_storage_provider):
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer"))
    assert await adopt_legacy_default_workspace(fake_storage_provider) == "primer"
    row = await ws_store.get("primer")
    await ws_store.update(row.model_copy(update={"name": "home"}))
    assert await adopt_legacy_default_workspace(fake_storage_provider) is None
    assert (await ws_store.get("primer")).name == "home"


async def test_adoption_never_acts_once_an_id_is_recorded(fake_storage_provider):
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer"))
    await fake_storage_provider.set_default_workspace_id("somewhere-else")
    assert await adopt_legacy_default_workspace(fake_storage_provider) is None
    assert (await ws_store.get("primer")).name == "primer"
    assert (await fake_storage_provider.get_system_state()).default_workspace_id == "somewhere-else"


async def test_adoption_does_not_write_a_name_that_is_already_prime(fake_storage_provider):
    """A pass that died between the rename and the record: the retry records, and does not write the name again."""
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer").model_copy(update={"name": "prime"}))
    writes: list[object] = []
    real_patch = ws_store.patch_if

    async def _spy(*args, **kwargs):
        writes.append(args)
        return await real_patch(*args, **kwargs)

    ws_store.patch_if = _spy  # type: ignore[method-assign]
    assert await adopt_legacy_default_workspace(fake_storage_provider) == "primer"
    assert writes == []
    assert (await fake_storage_provider.get_system_state()).default_workspace_id == "primer"


async def test_adoption_writes_only_the_name_with_a_guard_on_the_name_it_read(fake_storage_provider):
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer"))
    calls: list[tuple] = []
    real_patch = ws_store.patch_if

    async def _spy(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        calls.append((id, dict(patch or {}), dict(where), set_paths))
        return await real_patch(id, patch, where=where, set_paths=set_paths, conn=conn)

    async def _no_update(*args, **kwargs):
        raise AssertionError("the rename must be a field-scoped patch_if, never a whole-document update")

    ws_store.patch_if = _spy  # type: ignore[method-assign]
    ws_store.update = _no_update  # type: ignore[method-assign]
    await adopt_legacy_default_workspace(fake_storage_provider)
    assert calls == [("primer", {"name": "prime"}, {"name": ["primer"]}, None)]


async def test_a_rename_that_lands_between_the_read_and_the_write_is_kept(fake_storage_provider):
    """A person renames the workspace while the pass is adopting it: their name wins, and the record is still written."""
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer"))
    real_get = ws_store.get

    async def _get_then_rename(id, *, conn=None):  # noqa: A002
        row = await real_get(id, conn=conn)
        if row is not None and row.name == "primer":
            ws_store._data[id] = row.model_copy(update={"name": "my home"})
        return row

    ws_store.get = _get_then_rename  # type: ignore[method-assign]
    assert await adopt_legacy_default_workspace(fake_storage_provider) == "primer"
    assert (await real_get("primer")).name == "my home"
    assert (await fake_storage_provider.get_system_state()).default_workspace_id == "primer"


async def test_a_legacy_row_deleted_while_adopting_is_not_recorded(fake_storage_provider):
    ws_store = fake_storage_provider.get_storage(Workspace)
    await ws_store.create(_row("primer"))
    real_get = ws_store.get

    async def _get_then_delete(id, *, conn=None):  # noqa: A002
        row = await real_get(id, conn=conn)
        ws_store._data.pop(id, None)
        return row

    ws_store.get = _get_then_delete  # type: ignore[method-assign]
    assert await adopt_legacy_default_workspace(fake_storage_provider) is None
    assert (await fake_storage_provider.get_system_state()).default_workspace_id is None


async def test_recording_never_replaces_an_id_that_is_already_recorded(fake_storage_provider):
    await record_default_workspace_id(fake_storage_provider, "prime")
    await record_default_workspace_id(fake_storage_provider, "other")
    assert (await fake_storage_provider.get_system_state()).default_workspace_id == "prime"


# ---- the rename on a real database leaves every other field alone -----------------------------------------------------------------


@pytest_asyncio.fixture
async def sqlite_sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "adopt.sqlite")))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


async def test_the_rename_on_a_real_row_changes_the_name_and_nothing_else(sqlite_sp):
    """Every field set, including a secret and a field the probe task writes: only ``name`` differs afterwards."""
    store = sqlite_sp.get_storage(Workspace)
    full = Workspace(
        id="primer",
        name="primer",
        template_id="k3s-default",
        provider_id="k3s",
        overrides=WorkspaceTemplateOverrides(env={"TOKEN": "s3cr3t"}),
        created_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        phase="running",
        last_probe_at=datetime(2026, 10, 18, 9, 30, 0, tzinfo=timezone.utc),
        last_probe_ok=True,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://10.0.0.7:5959", token="runtime-secret"),
        reply_binding=WorkspaceChannelLink(channel_id="chan-1"),
        terminal_user_access=True,
    )
    await store.create(full)
    before = await store.get("primer")
    assert await adopt_legacy_default_workspace(sqlite_sp) == "primer"
    after = await store.get("primer")
    assert after.name == "prime"
    assert after.model_dump() | {"name": None} == before.model_dump() | {"name": None}
    assert after.runtime_meta.token.get_secret_value() == "runtime-secret"
    assert (await sqlite_sp.get_system_state()).default_workspace_id == "primer"


async def test_the_rename_does_not_overwrite_a_probe_write_that_lands_after_the_read(sqlite_sp):
    """The probe task rewrites ``phase`` and ``last_probe_*`` on every tick; a whole-document write would revert them."""
    store = sqlite_sp.get_storage(Workspace)
    row = Workspace(
        id="primer",
        name="primer",
        template_id="local-default",
        provider_id="local",
        created_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        phase="pending",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
    )
    await store.create(row)
    real_get = store.get

    async def _get_then_probe(id, *, conn=None):  # noqa: A002
        seen = await real_get(id, conn=conn)
        if seen is not None and seen.phase == "pending":
            await store.patch_if(id, {"phase": "failed", "failure_reason": "probe says no"}, where={"phase": ["pending"]})
        return seen

    store.get = _get_then_probe  # type: ignore[method-assign]
    assert await adopt_legacy_default_workspace(sqlite_sp) == "primer"
    final = await real_get("primer")
    assert (final.name, final.phase, final.failure_reason) == ("prime", "failed", "probe says no")
