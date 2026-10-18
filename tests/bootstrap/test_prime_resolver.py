"""``resolve_default_workspace_id`` (pivot P2 Task 1): the one reader of the default workspace.

The recorded ``system_state.default_workspace_id`` wins. While it is unset, the legacy ``primer`` row (what an
install created before the field existed has) is the default; with neither, there is no default yet. Nothing
else in the code base may hard-code the id, so this is what the delete guard, ``is_default`` and the console read.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.bootstrap.defaults import (
    LEGACY_DEFAULT_WORKSPACE_ID,
    PRIME_WORKSPACE_ID,
    PRIME_WORKSPACE_NAME,
)
from primer.bootstrap.prime import resolve_default_workspace_id
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta


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
