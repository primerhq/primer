"""Migration 27: existing MCP OAuth token sets become per-user (access-control plan P5 Task 2, Decision 4 and the lead's binding answer).

A browser token set stored under the bare toolset id becomes the sign-in of the user who connected it (``connected_by``, a username), else of the first
admin, else (no users: the auth-disabled shape) it stays under the bare id. Client-credentials rows stay the shared row. Every token row is stamped
with ``toolset_id`` and ``user_id``, every pending sign-in is dropped (ten-minute TTL), and a second run changes nothing. Run on real SQLite storage
through ``run_migrations``, the way a boot runs it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest_asyncio
from pydantic import ConfigDict

from primer.model.common import Identifiable
from primer.model.mcp_oauth import McpOAuthClient
from primer.model.provider import SqliteConfig
from primer.model.storage import CursorPage
from primer.storage.migrations import LATEST_VERSION, MIGRATIONS, run_migrations
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.oauth.store import token_key

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def _raw_view(table: str) -> type[Identifiable]:
    """A model whose class name selects ``table`` and that stores whatever it is given: rows as an older build wrote them."""
    return type(table, (Identifiable,), {"model_config": ConfigDict(extra="allow"), "__module__": __name__})


TOKENS = _raw_view("McpOAuthTokenSet")
PENDING = _raw_view("McpOAuthPendingSignIn")
USERS = _raw_view("User")


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m027.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _legacy_tokens(toolset_id: str, *, grant: str = "authorization_code", connected_by: str | None = "alice") -> Identifiable:
    """A token row as the shared-sign-in build wrote it: the id is the toolset id, no ``toolset_id`` / ``user_id``."""
    return TOKENS(
        id=toolset_id, grant=grant, access_token_enc="v1:sealed-access", refresh_token_enc="v1:sealed-refresh", token_endpoint="https://as.example/token",
        client_id="client-1", client_source="registered", config_fingerprint="f" * 64, connected_at=NOW.isoformat(), connected_by=connected_by, version=3,
    )


async def _user(sp, uid: str, username: str, role: str, age_days: int) -> None:
    await sp.get_storage(USERS).create(USERS(id=uid, username=username, role=role, created_at=(NOW - timedelta(days=age_days)).isoformat()))


async def _rows(sp, view) -> dict[str, dict[str, Any]]:
    store = sp.get_storage(view)
    out: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        page = await store.list(CursorPage(cursor=cursor, length=100))
        out.update({row.id: row.model_dump(mode="json") for row in page.items})
        cursor = page.next_cursor
        if cursor is None:
            return out


async def _an_install(sp, *, users: bool = True) -> None:
    """Schema 26 with three browser token sets (connected by alice, by a username nobody holds any more, by nobody named), a client-credentials one,
    a registered client and two pending sign-ins."""
    if users:
        await _user(sp, "u-root", "root", "admin", age_days=30)       # the first admin
        await _user(sp, "u-late", "late", "admin", age_days=1)
        await _user(sp, "u-alice", "alice", "admin", age_days=10)
        await _user(sp, "u-bob", "bob", "user", age_days=5)
    tokens = sp.get_storage(TOKENS)
    await tokens.create(_legacy_tokens("kb", connected_by="alice"))
    await tokens.create(_legacy_tokens("gh", connected_by="ghost"))
    await tokens.create(_legacy_tokens("nb", connected_by=None))
    await tokens.create(_legacy_tokens("cc", grant="client_credentials", connected_by="alice"))
    await sp.get_storage(McpOAuthClient).create(McpOAuthClient(
        id="kb", issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="client-1", registered_at=NOW,
    ))
    for n in (1, 2):
        await sp.get_storage(PENDING).create(PENDING(id=f"pending-{n}", toolset_id="kb", started_by="alice", consumed=False))
    await sp.set_schema_version(26)


def _moved(before: dict[str, Any], key: str, toolset_id: str, user_id: str | None) -> dict[str, Any]:
    return {**before, "id": key, "toolset_id": toolset_id, "user_id": user_id}


async def test_connected_by_user_wins_and_unknown_username_goes_to_the_first_admin(sp):
    await _an_install(sp)
    before = await _rows(sp, TOKENS)

    assert await run_migrations(sp, is_fresh_install=False) == LATEST_VERSION

    after = await _rows(sp, TOKENS)
    assert after == {
        token_key("kb", "u-alice"): _moved(before["kb"], token_key("kb", "u-alice"), "kb", "u-alice"),
        token_key("gh", "u-root"): _moved(before["gh"], token_key("gh", "u-root"), "gh", "u-root"),
        token_key("nb", "u-root"): _moved(before["nb"], token_key("nb", "u-root"), "nb", "u-root"),
        "cc": _moved(before["cc"], "cc", "cc", None),
    }, "a browser row moves to its heir with its tokens as stored; client credentials stay the shared row"


async def test_no_users_leaves_the_row_under_the_bare_id(sp):
    await _an_install(sp, users=False)
    before = await _rows(sp, TOKENS)

    await run_migrations(sp, is_fresh_install=False)

    assert await _rows(sp, TOKENS) == {key: _moved(row, key, key, None) for key, row in before.items()}


async def test_client_credentials_and_the_registered_client_untouched_and_pending_dropped(sp):
    await _an_install(sp)
    clients = await _rows(sp, McpOAuthClient)

    await run_migrations(sp, is_fresh_install=False)

    assert await _rows(sp, McpOAuthClient) == clients
    assert await _rows(sp, PENDING) == {}
    assert (await _rows(sp, TOKENS))["cc"]["user_id"] is None


async def test_second_run_changes_nothing(sp):
    from primer.storage.migrations.m027_mcp_oauth_per_user import M027McpOAuthPerUser

    await _an_install(sp)
    await run_migrations(sp, is_fresh_install=False)
    once = {view.__name__: await _rows(sp, view) for view in (TOKENS, PENDING, McpOAuthClient)}

    await M027McpOAuthPerUser().apply(sp)
    await sp.set_schema_version(26)
    await run_migrations(sp, is_fresh_install=False)

    assert {view.__name__: await _rows(sp, view) for view in (TOKENS, PENDING, McpOAuthClient)} == once


async def test_a_run_that_died_after_the_copy_finishes_the_move(sp):
    """The heir's row was written and the bare row not yet deleted: the next run deletes the bare row and keeps the copy as it is."""
    await _an_install(sp)
    before = await _rows(sp, TOKENS)
    key = token_key("kb", "u-alice")
    await sp.get_storage(TOKENS).create(TOKENS(**_moved(before["kb"], key, "kb", "u-alice")))

    await run_migrations(sp, is_fresh_install=False)

    after = await _rows(sp, TOKENS)
    assert "kb" not in after and after[key] == _moved(before["kb"], key, "kb", "u-alice")


def test_it_is_registered_as_version_27():
    from primer.storage.migrations.m027_mcp_oauth_per_user import M027McpOAuthPerUser

    assert isinstance(MIGRATIONS[26], M027McpOAuthPerUser) and MIGRATIONS[26].version == 27
