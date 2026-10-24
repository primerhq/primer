"""Migration 17: an upgraded install forgets the MCP OAuth sign-ins that toolsets deleted before #852 left behind (ticket 01a16a36, L2).

The system ``delete_toolset`` tool and the harness uninstall and sync used to delete a toolset and keep its token row, its registered client and its
pending sign-ins, so a toolset created again under the same id came back connected on the old tokens. The step deletes every such row whose toolset
no longer exists and never touches one whose toolset does. Run on real SQLite storage through ``run_migrations``, the way a boot runs it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ConfigDict

from primer.common.secret_box import SecretBox
from primer.model.common import Identifiable
from primer.model.mcp_oauth import McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet
from primer.model.provider import SqliteConfig, Toolset
from primer.model.storage import CursorPage
from primer.storage.migrations import LATEST_VERSION, MIGRATIONS, run_migrations
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.oauth.store import McpOAuthStore, pending_key

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
KEY = "m017-test-key-0123456789"
_OAUTH_MODELS = (McpOAuthTokenSet, McpOAuthClient, McpOAuthPendingSignIn)


async def _provider(path: Path) -> SqliteStorageProvider:
    provider = SqliteStorageProvider(SqliteConfig(path=path))
    await provider.initialize()
    return provider


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = await _provider(tmp_path / "m017.sqlite")
    try:
        yield provider
    finally:
        await provider.aclose()


def _raw_view(table: str) -> type[Identifiable]:
    """A model whose class name selects ``table`` and that stores whatever it is given: for rows the live models would refuse."""
    return type(table, (Identifiable,), {"model_config": ConfigDict(extra="allow"), "__module__": __name__})


def _toolset(toolset_id: str) -> Toolset:
    return Toolset.model_validate({
        "id": toolset_id, "provider": "mcp",
        "config": {"transport": "http", "config": {"url": f"https://{toolset_id}.example/mcp", "oauth": {"grant": "authorization_code"}}},
    })


async def _sign_in(store: McpOAuthStore, toolset_id: str, *, tokens: bool = True, client: bool = True, pending: tuple[str, ...] = ("live",)) -> None:
    """Store a sign-in for ``toolset_id`` the way the service does: tokens, a registered client and pending sign-ins (one per state suffix)."""
    if tokens:
        await store.replace_tokens(McpOAuthTokenSet(
            id=toolset_id, grant="authorization_code", access_token_enc=store.seal("tok"), refresh_token_enc=store.seal("ref"),
            token_endpoint="https://as.example/token", client_id="client-1", client_source="registered", config_fingerprint="f" * 64,
            connected_at=NOW, connected_by="admin",
        ))
    if client:
        await store.put_client(McpOAuthClient(
            id=toolset_id, issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="client-1", registered_at=NOW,
        ))
    for suffix in pending:
        expires = NOW - timedelta(minutes=1) if suffix == "expired" else NOW + timedelta(minutes=10)
        await store.put_pending(McpOAuthPendingSignIn(
            id=f"{toolset_id}-{suffix}", toolset_id=toolset_id, code_verifier_enc=store.seal("verifier"), redirect_uri="https://p.example/cb",
            issuer="https://as.example", token_endpoint="https://as.example/token", client_id="client-1", client_source="registered",
            config_fingerprint="f" * 64, created_at=NOW, expires_at=expires,
        ))


async def _oauth_rows(sp) -> dict[str, dict[str, dict[str, Any]]]:
    """Every stored OAuth row, by table then id, as stored (sealed values as their ciphertext)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for model in _OAUTH_MODELS:
        store = sp.get_storage(model)
        rows: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        while True:
            page = await store.list(CursorPage(cursor=cursor, length=100))
            rows.update({row.id: row.model_dump(mode="json") | _sealed(row) for row in page.items})
            cursor = page.next_cursor
            if cursor is None:
                break
        out[model.__name__] = rows
    return out


def _sealed(row: Any) -> dict[str, Any]:
    """The sealed fields as their ciphertext, so two snapshots differ when a sealed value was rewritten (a JSON dump masks them)."""
    names = ("access_token_enc", "refresh_token_enc", "client_secret_enc", "code_verifier_enc")
    return {name: value.get_secret_value() for name in names if (value := getattr(row, name, None)) is not None}


def _ids(rows: dict[str, dict[str, dict[str, Any]]]) -> dict[str, set[str]]:
    return {table: set(by_id) for table, by_id in rows.items()}


async def _an_install_with_orphans(sp) -> McpOAuthStore:
    """Schema 16 (before this step) with one live toolset ``kb`` signed in, and the leftovers of deleted toolsets:

    ``gone`` (tokens, client, a live and an expired pending sign-in), ``tokens-only`` (a token row alone) and ``pending-only`` (one pending sign-in).
    """
    store = McpOAuthStore(sp, SecretBox.from_key_material(KEY))
    await sp.get_storage(Toolset).create(_toolset("kb"))
    await _sign_in(store, "kb", pending=("live", "expired"))
    await _sign_in(store, "gone", pending=("live", "expired"))
    await _sign_in(store, "tokens-only", client=False, pending=())
    await _sign_in(store, "pending-only", tokens=False, client=False)
    await sp.set_schema_version(16)
    return store


_KB_ONLY = {
    "McpOAuthTokenSet": {"kb"},
    "McpOAuthClient": {"kb"},
    "McpOAuthPendingSignIn": {pending_key("kb-live"), pending_key("kb-expired")},
}


async def test_an_upgraded_install_forgets_the_orphans_and_keeps_the_live_toolsets_rows_as_they_were(sp):
    await _an_install_with_orphans(sp)
    before = await _oauth_rows(sp)

    assert await run_migrations(sp, is_fresh_install=False) == LATEST_VERSION

    after = await _oauth_rows(sp)
    assert _ids(after) == _KB_ONLY, "every row of a deleted toolset goes; the live toolset's stay"
    for table, ids in _KB_ONLY.items():
        assert {i: after[table][i] for i in ids} == {i: before[table][i] for i in ids}, f"{table}: a live toolset's row is not rewritten"


async def test_it_is_idempotent(sp):
    """A second run (a boot that died after the step and before the version stamp, or the step applied again) deletes nothing more."""
    from primer.storage.migrations.m017_orphan_mcp_oauth_rows import M017OrphanMcpOAuthRows

    await _an_install_with_orphans(sp)
    await run_migrations(sp, is_fresh_install=False)
    once = await _oauth_rows(sp)

    await sp.set_schema_version(16)
    assert await run_migrations(sp, is_fresh_install=False) == LATEST_VERSION
    await M017OrphanMcpOAuthRows().apply(sp)

    assert await _oauth_rows(sp) == once
    assert _ids(once) == _KB_ONLY


async def test_a_fresh_install_and_an_upgraded_one_end_the_same(tmp_path):
    """A fresh install baselines past the step; an upgraded one runs it. With the same live toolset connected, both end with the same rows."""
    fresh = await _provider(tmp_path / "fresh.sqlite")
    upgraded = await _provider(tmp_path / "upgraded.sqlite")
    try:
        assert await run_migrations(fresh, is_fresh_install=True) == LATEST_VERSION
        await fresh.get_storage(Toolset).create(_toolset("kb"))
        await _sign_in(McpOAuthStore(fresh, SecretBox.from_key_material(KEY)), "kb", pending=("live", "expired"))

        await _an_install_with_orphans(upgraded)
        assert await run_migrations(upgraded, is_fresh_install=False) == LATEST_VERSION

        assert _ids(await _oauth_rows(upgraded)) == _ids(await _oauth_rows(fresh)) == _KB_ONLY
        assert (await upgraded.get_system_state()).schema_version == (await fresh.get_system_state()).schema_version == LATEST_VERSION
    finally:
        await fresh.aclose()
        await upgraded.aclose()


async def test_a_toolset_the_live_model_would_refuse_still_counts_as_existing(sp):
    """The step reads toolsets through its own view: a stored row of an older shape is a toolset, so its sign-in is kept (and the boot does not
    fail on it)."""
    store = McpOAuthStore(sp, SecretBox.from_key_material(KEY))
    await sp.get_storage(_raw_view("Toolset")).create(_raw_view("Toolset")(id="legacy", provider="retired-kind", config={"anything": 1}))
    await _sign_in(store, "legacy")
    await sp.set_schema_version(16)
    before = await _oauth_rows(sp)

    await run_migrations(sp, is_fresh_install=False)

    assert await _oauth_rows(sp) == before


async def test_a_pending_sign_in_that_names_no_toolset_is_left_alone(sp):
    """A pending row with no ``toolset_id`` (no build wrote one; a damaged row) names nobody's toolset, so the step does not decide it is an
    orphan."""
    view = _raw_view("McpOAuthPendingSignIn")
    await sp.get_storage(view).create(view(id="no-toolset", consumed=False))
    await sp.set_schema_version(16)

    await run_migrations(sp, is_fresh_install=False)

    assert await sp.get_storage(view).get("no-toolset") is not None


def test_it_is_registered_as_version_17():
    from primer.storage.migrations.m017_orphan_mcp_oauth_rows import M017OrphanMcpOAuthRows

    assert isinstance(MIGRATIONS[16], M017OrphanMcpOAuthRows)
    assert MIGRATIONS[16].version == 17


@pytest.mark.parametrize("model", _OAUTH_MODELS, ids=lambda m: m.__name__)
def test_the_frozen_views_select_the_live_tables(model):
    """Each view's class name is the table name: when the step shipped, they named the live models' tables."""
    from primer.storage.migrations import m017_orphan_mcp_oauth_rows as m017

    assert getattr(m017, model.__name__).__name__ == model.__name__
    assert m017.Toolset.__name__ == Toolset.__name__
    assert "toolset_id" in McpOAuthPendingSignIn.model_fields, "the pending row names its toolset in toolset_id"
