"""A harness sync that changes an OAuth toolset's URL or OAuth settings forgets its shared sign-in (ticket 01a16a36, L4 of the #852 review).

The REST update and the system ``update_toolset`` tool forget the sign-in when ``oauth_settings_changed`` says the URL or the OAuth block changed
(the #852 rule), so the old tokens never follow the toolset to another server or another sign-in setup. A harness sync writes toolset rows straight
to storage and kept the tokens, the registered client and the pending sign-ins. It applies the same rule through the same predicate now, and
forgets before the row is written, as the other two do. Run on real SQLite storage.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from primer.common.secret_box import SecretBox
from primer.harness.service import apply_sync
from primer.model.harness import Harness, HarnessRendering, RenderedEntry
from primer.model.mcp_oauth import McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig, Toolset
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.oauth.store import McpOAuthStore, pending_key

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
KEY = "harness-sync-oauth-key-0123456789"
_ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
_URL = "https://mcp.example/mcp"
_OAUTH: dict[str, Any] = {"grant": "authorization_code", "scopes": ["read"]}


@pytest_asyncio.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "harness-oauth.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


def _payload(url: str = _URL, oauth: dict[str, Any] | None = _OAUTH, team: str = "a", transport: str = "http") -> dict[str, Any]:
    http: dict[str, Any] = {"url": url, "headers": {"X-Team": team}}
    if oauth is not None:
        http["oauth"] = oauth
    return {"provider": "mcp", "config": {"transport": transport, "config": http}}


def _entry(payload: dict[str, Any], rendered_hash: str, resolved_id: str = "acme__ts") -> RenderedEntry:
    return RenderedEntry(
        kind="toolset", template_name=resolved_id.split("__")[-1], resolved_id=resolved_id,
        template_source_hash="h", rendered_hash=rendered_hash, rendered_payload=payload,
    )


def _harness() -> Harness:
    return Harness(
        id="acme-id", slug="acme", name="Acme", git_url="https://github.com/x/y",
        operation_requested_by=_ADMIN, created_at=NOW,
    )


async def _sign_in(store: McpOAuthStore, toolset_id: str) -> None:
    """Store a whole sign-in for ``toolset_id``: its tokens, a registered client and a pending browser sign-in."""
    await store.replace_tokens(McpOAuthTokenSet(
        id=toolset_id, grant="authorization_code", access_token_enc=store.seal("tok"), refresh_token_enc=store.seal("ref"),
        token_endpoint="https://as.example/token", client_id="client-1", client_source="registered", config_fingerprint="f" * 64,
        connected_at=NOW, connected_by="admin",
    ))
    await store.put_client(McpOAuthClient(
        id=toolset_id, issuer="https://as.example", redirect_uri="https://p.example/cb", client_id="client-1", registered_at=NOW,
    ))
    await store.put_pending(McpOAuthPendingSignIn(
        id=f"state-{toolset_id}", toolset_id=toolset_id, code_verifier_enc=store.seal("verifier"), redirect_uri="https://p.example/cb",
        issuer="https://as.example", token_endpoint="https://as.example/token", client_id="client-1", client_source="registered",
        config_fingerprint="f" * 64, created_at=NOW, expires_at=NOW + timedelta(minutes=10),
    ))


async def _signed_in(sp, store: McpOAuthStore, toolset_id: str) -> tuple[bool, bool, bool]:
    """Whether the toolset still has (tokens, a registered client, its pending sign-in)."""
    pending = await sp.get_storage(McpOAuthPendingSignIn).get(pending_key(f"state-{toolset_id}"))
    return (await store.get_tokens(toolset_id) is not None, await store.get_client(toolset_id) is not None, pending is not None)


async def _installed(sp, entries: list[RenderedEntry]) -> McpOAuthStore:
    """An install of ``entries`` (rendering and rows), each toolset signed in."""
    await sp.get_storage(HarnessRendering).create(HarnessRendering(
        id="acme-id", harness_id="acme-id", bundle_hash="old", overrides_hash="oh", schema_hash=None, entries=entries, rendered_at=NOW,
    ))
    store = McpOAuthStore(sp, SecretBox.from_key_material(KEY))
    for entry in entries:
        await sp.get_storage(Toolset).create(Toolset.model_validate({"id": entry.resolved_id, **entry.rendered_payload, "harness_id": "acme-id"}))
        await _sign_in(store, entry.resolved_id)
    return store


async def _sync(sp, entries: list[RenderedEntry]) -> str | None:
    return await apply_sync(
        storage_provider=sp, harness=_harness(), new_entries=entries, rendered_files_by_name={},
        bundle_hash="new", overrides_hash="oh", schema_hash=None,
    )


@pytest.mark.parametrize(
    "changed",
    [
        _payload(url="https://elsewhere.example/mcp"),
        _payload(oauth={"grant": "authorization_code", "scopes": ["read", "write"]}),
        _payload(oauth={"grant": "authorization_code", "scopes": ["read"], "resource_uri": "https://mcp.example/"}),
        _payload(oauth=None),
    ],
    ids=["url", "scopes", "resource-uri", "oauth-removed"],
)
async def test_a_sync_that_changes_the_url_or_the_oauth_settings_forgets_the_sign_in(sp, changed):
    other = _entry(_payload(url="https://other.example/mcp"), "o1", resolved_id="acme__other")
    store = await _installed(sp, [_entry(_payload(), "r-old"), other])

    assert await _sync(sp, [_entry(changed, "r-new"), other]) is None

    stored = await sp.get_storage(Toolset).get("acme__ts")
    assert stored.config.config.url == changed["config"]["config"]["url"], "the sync wrote the change"
    assert await _signed_in(sp, store, "acme__ts") == (False, False, False), "tokens, registered client and pending sign-ins all go"
    assert await _signed_in(sp, store, "acme__other") == (True, True, True), "another toolset's sign-in is not touched"


@pytest.mark.parametrize(
    ("changed", "check"),
    [
        (_payload(team="b"), lambda row: row.config.config.headers["X-Team"].get_secret_value() == "b"),
        (_payload(transport="sse"), lambda row: row.config.transport.value == "sse"),
    ],
    ids=["headers", "transport"],
)
async def test_a_sync_that_changes_neither_the_url_nor_the_oauth_settings_keeps_the_sign_in(sp, changed, check):
    """The toolset is rewritten (a header or the transport, which the rule leaves out) and a second one is not touched at all: both stay signed
    in."""
    other = _entry(_payload(url="https://other.example/mcp"), "o1", resolved_id="acme__other")
    store = await _installed(sp, [_entry(_payload(), "r-old"), other])

    assert await _sync(sp, [_entry(changed, "r-new"), other]) is None

    assert check(await sp.get_storage(Toolset).get("acme__ts")), "the sync wrote the change"
    assert await _signed_in(sp, store, "acme__ts") == (True, True, True)
    assert await _signed_in(sp, store, "acme__other") == (True, True, True)


async def test_a_sync_that_gives_a_toolset_its_first_oauth_block_has_nothing_to_forget_and_keeps_others(sp):
    """A toolset that did not sign in with OAuth is outside the rule (``oauth_settings_changed`` is False for it), as on the REST update."""
    store = await _installed(sp, [_entry(_payload(oauth=None), "r-old")])

    assert await _sync(sp, [_entry(_payload(), "r-new")]) is None

    assert await _signed_in(sp, store, "acme__ts") == (True, True, True)


async def test_a_forget_that_fails_leaves_the_toolset_unchanged_and_the_sync_failed(sp, monkeypatch):
    """The sign-in is forgotten BEFORE the row is written (as the REST update and the system tool do), so a forget that fails writes nothing:
    the sync reports the toolset, keeps the old snapshot, and the next sync tries again. The new URL is never stored beside the old tokens."""
    store = await _installed(sp, [_entry(_payload(), "r-old")])

    async def _broken(storage_provider, toolset_id):
        raise RuntimeError("storage is down")

    monkeypatch.setattr("primer.harness.service._forget_toolset_sign_in", _broken)

    error = await _sync(sp, [_entry(_payload(url="https://elsewhere.example/mcp"), "r-new")])

    assert error is not None and json.loads(error)["code"] == "partial_apply_failure"
    assert (await sp.get_storage(Toolset).get("acme__ts")).config.config.url == _URL
    assert (await sp.get_storage(HarnessRendering).get("acme-id")).bundle_hash == "old", "the snapshot does not advance"
    assert await _signed_in(sp, store, "acme__ts") == (True, True, True)
