"""Migration 29: existing MCP OAuth token sets become per-user (access-control plan P5 Task 2, Decision 4 and the lead's binding answer).

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
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m029.sqlite"))
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
        # GR-14: the bare id stays a shared copy (stamped), which an auth-disabled install's system person still reads
        "kb": _moved(before["kb"], "kb", "kb", None),
        "gh": _moved(before["gh"], "gh", "gh", None),
        "nb": _moved(before["nb"], "nb", "nb", None),
    }, "a browser row is copied to its heir with its tokens as stored; the bare row stays as the shared copy; client credentials stay shared"


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
    from primer.storage.migrations.m029_mcp_oauth_per_user import M029McpOAuthPerUser

    await _an_install(sp)
    await run_migrations(sp, is_fresh_install=False)
    once = {view.__name__: await _rows(sp, view) for view in (TOKENS, PENDING, McpOAuthClient)}

    await M029McpOAuthPerUser().apply(sp)
    await sp.set_schema_version(26)
    await run_migrations(sp, is_fresh_install=False)

    assert {view.__name__: await _rows(sp, view) for view in (TOKENS, PENDING, McpOAuthClient)} == once


async def test_a_run_that_died_after_the_copy_finishes_the_stamp(sp):
    """The heir's row was written and the bare row not yet stamped: the next run stamps the bare row and keeps the copy as it is."""
    await _an_install(sp)
    before = await _rows(sp, TOKENS)
    key = token_key("kb", "u-alice")
    await sp.get_storage(TOKENS).create(TOKENS(**_moved(before["kb"], key, "kb", "u-alice")))

    await run_migrations(sp, is_fresh_install=False)

    after = await _rows(sp, TOKENS)
    assert after["kb"] == _moved(before["kb"], "kb", "kb", None) and after[key] == _moved(before["kb"], key, "kb", "u-alice")


def test_it_is_registered_as_version_29():
    from primer.storage.migrations.m029_mcp_oauth_per_user import M029McpOAuthPerUser

    assert isinstance(MIGRATIONS[28], M029McpOAuthPerUser) and MIGRATIONS[28].version == 29


async def test_the_heir_is_an_id_or_an_enabled_admin(sp):
    """Review finding 3: ``connected_by`` resolves as a user id first; a username counts only for an enabled admin (a demoted or disabled one, or a
    plain user who took the name later, never inherits the upstream account); else the first ENABLED admin; else the row stays under the bare id."""
    await sp.get_storage(USERS).create(USERS(id="u-root", username="root", role="admin", disabled=True, created_at=(NOW - timedelta(days=30)).isoformat()))
    await _user(sp, "u-ann", "ann", "admin", age_days=20)               # the first enabled admin
    await _user(sp, "u-bob", "bob", "user", age_days=5)
    await sp.get_storage(USERS).create(USERS(id="u-old", username="old", role="admin", disabled=True, created_at=NOW.isoformat()))
    await _user(sp, "u-sly", "u-bob", "admin", age_days=1)              # a username that spells another user's id
    tokens = sp.get_storage(TOKENS)
    for toolset_id, named in (("by-id", "u-bob"), ("by-user-name", "bob"), ("by-disabled-admin", "old"), ("by-admin-name", "ann")):
        await tokens.create(_legacy_tokens(toolset_id, connected_by=named))
    await sp.set_schema_version(26)

    await run_migrations(sp, is_fresh_install=False)

    heirs = {row["toolset_id"]: row["user_id"] for row in (await _rows(sp, TOKENS)).values()}
    assert heirs == {"by-id": "u-bob", "by-user-name": "u-ann", "by-disabled-admin": "u-ann", "by-admin-name": "u-ann"}


async def test_no_enabled_admin_leaves_the_row(sp):
    await sp.get_storage(USERS).create(USERS(id="u-root", username="root", role="admin", disabled=True, created_at=NOW.isoformat()))
    await _user(sp, "u-bob", "bob", "user", age_days=5)
    await sp.get_storage(TOKENS).create(_legacy_tokens("kb", connected_by="root"))
    await sp.set_schema_version(26)

    await run_migrations(sp, is_fresh_install=False)

    assert {key: (row["toolset_id"], row["user_id"]) for key, row in (await _rows(sp, TOKENS)).items()} == {"kb": ("kb", None)}


async def test_the_bare_id_shared_row_survives_the_move(sp):
    """GR-14: an install that once had auth on (so users exist) and now has it off acts as the system person, which reads only the bare-id row."""
    await _an_install(sp)
    await run_migrations(sp, is_fresh_install=False)
    after = await _rows(sp, TOKENS)
    for toolset_id in ("kb", "gh", "nb"):
        assert after[toolset_id]["toolset_id"] == toolset_id and after[toolset_id]["user_id"] is None
        assert after[toolset_id]["access_token_enc"] == "v1:sealed-access"


# -- golden characterization (PERF-T16) ---------------------------------------------------------------------------------------------------

import pytest  # noqa: E402

from primer.model.except_ import ConflictError, NotFoundError  # noqa: E402
from primer.storage.migrations._rows import read_all  # noqa: E402
from primer.storage.migrations.m029_mcp_oauth_per_user import M029McpOAuthPerUser, _token_key  # noqa: E402
from primer.storage.migrations.m029_mcp_oauth_per_user import McpOAuthPendingSignIn as PendingView  # noqa: E402
from primer.storage.migrations.m029_mcp_oauth_per_user import McpOAuthTokenSet as TokenView  # noqa: E402
from primer.storage.migrations.m029_mcp_oauth_per_user import User as UserView  # noqa: E402
from tests.storage._migration_golden import assert_golden, bulk, insert_raw  # noqa: E402


async def _old_apply(sp) -> None:
    """Migration 29 as it was before PERF-T16, frozen: read the three tables whole, then copy, stamp and delete."""
    tokens = sp.get_storage(TokenView)
    pending = sp.get_storage(PendingView)
    users = await read_all(sp.get_storage(UserView))
    token_rows = await read_all(tokens)
    pending_rows = await read_all(pending)
    by_id = {u.id for u in users}
    admins = [u for u in users if u.role == "admin" and not u.disabled]
    by_name = {u.username: u.id for u in admins if u.username}
    dated = [u for u in admins if u.created_at is not None]
    first_admin = min(dated, key=lambda u: u.created_at).id if dated else None
    for row in token_rows:
        if row.toolset_id is not None:
            continue
        heir = None
        if row.grant != "client_credentials":
            named = row.connected_by
            heir = (named if named in by_id else None) or by_name.get(named) or first_admin
        if heir is None:
            await tokens.update(row.model_copy(update={"toolset_id": row.id, "user_id": None}))
            continue
        try:
            await tokens.create(row.model_copy(update={"id": _token_key(row.id, heir), "toolset_id": row.id, "user_id": heir}))
        except ConflictError:
            pass
        await tokens.update(row.model_copy(update={"toolset_id": row.id, "user_id": None}))
    for row in pending_rows:
        try:
            await pending.delete(row.id)
        except NotFoundError:
            continue


def _golden_seed(with_users: bool):
    async def seed(sp) -> None:
        await insert_raw(sp, UserView, {
            "admin-new": {"username": "boss", "role": "admin", "created_at": "2025-06-01T00:00:00Z"},
            "admin-old": {"username": "elder", "role": "admin", "created_at": "2025-01-01T00:00:00Z"},
            "admin-ghost": {"username": "ghost", "role": "admin", "disabled": True, "created_at": "2024-01-01T00:00:00Z"},
            "admin-undated": {"username": "nodate", "role": "admin"},
            "u1": {"username": "carol", "role": "user"},
            **bulk("ub", 450, lambda i: {"role": "user"}),
        } if with_users else {})
        # Browser rows copy to keys "u-<sha256>", which sort after every "ts-" id: the step creates rows ahead of its own cursor.
        await insert_raw(sp, TokenView, {
            "ts-a": {"grant": "authorization_code", "connected_by": "u1", "sealed": "ciphertext", "version": 3},
            "ts-b": {"grant": "authorization_code", "connected_by": "boss"},
            "ts-c": {"grant": "authorization_code", "connected_by": "carol"},
            "ts-d": {"grant": "authorization_code", "connected_by": "ghost"},
            "ts-e": {"grant": "client_credentials", "connected_by": "u1"},
            "ts-f": {"grant": "authorization_code", "toolset_id": "ts-f", "user_id": None},
            "ts-g": {"lease": {"until": None}},
            "ts-h": {"grant": "authorization_code", "connected_by": "u1"},
            _token_key("ts-h", "u1"): {"grant": "authorization_code", "toolset_id": "ts-h", "user_id": "u1", "copied": "earlier run"},
            **bulk("ts-b", 450, lambda i: {"grant": "authorization_code", "connected_by": "u1" if i % 2 else "nodate", "n": i}),
        })
        await insert_raw(sp, PendingView, {"p1": {"toolset_id": "ts-a"}, "p2": {}, **bulk("pb", 450, lambda i: {"state": i})})
    return seed


@pytest.mark.parametrize("with_users", [True, False])
async def test_golden_rows_match_the_old_implementation_and_a_second_run_changes_nothing(tmp_path: Path, with_users: bool) -> None:
    await assert_golden(tmp_path, _golden_seed(with_users), (TokenView, PendingView, UserView), _old_apply, M029McpOAuthPerUser().apply)
