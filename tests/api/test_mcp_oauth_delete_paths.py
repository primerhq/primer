"""Every path that deletes a toolset forgets its shared OAuth sign-in (audit G4-02).

The REST delete always did (``_toolset_on_delete``). The system tool ``delete_toolset`` and the harness uninstall and sync deleted the toolset row only,
so its tokens (the refresh token among them), its registered client and its pending sign-ins stayed behind, and a toolset created again with the same
id and settings was connected on the old sign-in without an admin connecting it. Each path below deletes a connected OAuth toolset that also has a
registered client and a pending sign-in, then the toolset is created again as it was: nothing of the old sign-in may be left, and the new toolset needs
a sign-in.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from primer.api.registries.provider_registry import _build_default_toolset_factory
from primer.common.secret_box import SecretBox
from primer.harness.service import apply_sync, apply_uninstall
from primer.model.harness import Harness, HarnessRendering, RenderedEntry
from primer.model.mcp_oauth import McpOAuthClient, McpOAuthPendingSignIn
from primer.model.provider import Toolset
from primer.toolset.oauth.service import ToolsetOAuthService, config_fingerprint
from primer.toolset.oauth.store import pending_key
from primer.toolset.system import build_system_toolset
from tests._support.caller import caller
from tests.api.test_require_user_admin import _login, _seed
from tests.toolset.oauth._service_helpers import target_of, token_row

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
HARNESS_ID = "acme-id"
STATE = "pending-state-of-the-deleted-toolset"


def _payload() -> dict:
    return {
        "provider": "mcp",
        "config": {"transport": "http", "config": {"url": "https://mcp.example/mcp", "oauth": {"grant": "authorization_code", "scopes": ["read"]}}},
    }


def _toolset(toolset_id: str, *, harness_id: str | None = None) -> Toolset:
    return Toolset.model_validate({"id": toolset_id, **_payload(), "harness_id": harness_id})


@pytest.fixture
async def oauth_app(app, fake_storage_provider, fake_provider_registry):
    await app.state.mcp_oauth.aclose()
    service = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=SecretBox.from_key_material("delete-paths-key-0123456789"))
    app.state.mcp_oauth = service
    fake_provider_registry._toolset_factory = _build_default_toolset_factory(oauth=service)
    yield app
    await service.aclose()


async def _connected(service: ToolsetOAuthService, toolset: Toolset) -> None:
    """A connected sign-in with everything a sign-in stores: the tokens, the registered client and a pending sign-in (a Reconnect left open)."""
    store, target = service.store, target_of(toolset)
    await store.replace_tokens(token_row(store, target))
    await store.put_client(McpOAuthClient(
        id=toolset.id, issuer="https://as.example", redirect_uri="https://primer.example/cb", client_id="c1", registered_at=NOW,
    ))
    await store.put_pending(McpOAuthPendingSignIn(
        id=STATE, toolset_id=toolset.id, code_verifier_enc=store.seal("verifier"), redirect_uri="https://primer.example/cb",
        issuer="https://as.example", token_endpoint="https://as.example/token", client_id="c1", client_source="registered",
        config_fingerprint=config_fingerprint(target), created_at=NOW, expires_at=NOW + timedelta(minutes=10),
    ))
    assert (await service.status(toolset)).state == "connected"


async def _by_rest(app, raw_client, toolset: Toolset) -> None:
    await _seed(app, uid="u-admin", username="admin", role="admin")
    await _login(raw_client, "admin")
    response = await raw_client.delete(f"/v1/toolsets/{toolset.id}")
    assert response.status_code == 204, response.text


async def _by_the_system_tool(app, raw_client, toolset: Toolset) -> None:
    del raw_client
    tools = build_system_toolset(
        storage_provider=app.state.storage_provider, provider_registry=app.state.provider_registry, mcp_oauth=app.state.mcp_oauth,
    )
    result = await tools.call(tool_name="delete_toolset", arguments={"id": toolset.id}, ctx=caller("user"))
    assert not result.is_error, result.output


def _harness() -> Harness:
    return Harness(id=HARNESS_ID, slug="acme", name="Acme", git_url="https://github.com/x/y", created_at=NOW)


def _entry(toolset_id: str) -> RenderedEntry:
    return RenderedEntry(kind="toolset", template_name="ts", resolved_id=toolset_id, template_source_hash="h", rendered_hash="r1",
                         rendered_payload=_payload())


async def _rendered(storage_provider, toolset_id: str) -> None:
    await storage_provider.get_storage(Harness).create(_harness())
    await storage_provider.get_storage(HarnessRendering).create(HarnessRendering(
        id=HARNESS_ID, harness_id=HARNESS_ID, bundle_hash="old", overrides_hash="oh", schema_hash=None, entries=[_entry(toolset_id)], rendered_at=NOW,
    ))


async def _by_the_harness_uninstall(app, raw_client, toolset: Toolset) -> None:
    del raw_client
    await _rendered(app.state.storage_provider, toolset.id)
    await apply_uninstall(storage_provider=app.state.storage_provider, harness=_harness(), cascade=True)


async def _by_a_harness_sync_that_drops_it(app, raw_client, toolset: Toolset) -> None:
    del raw_client
    await _rendered(app.state.storage_provider, toolset.id)
    error = await apply_sync(
        storage_provider=app.state.storage_provider, harness=_harness(), new_entries=[], rendered_files_by_name={}, bundle_hash="new",
        overrides_hash="oh", schema_hash=None,
    )
    assert error is None, error


DeletePath = Callable[..., Awaitable[None]]
PATHS: dict[str, tuple[DeletePath, str | None]] = {
    "rest": (_by_rest, None),
    "system_tool": (_by_the_system_tool, None),
    "harness_uninstall": (_by_the_harness_uninstall, HARNESS_ID),
    "harness_sync": (_by_a_harness_sync_that_drops_it, HARNESS_ID),
}


@pytest.mark.parametrize("path", sorted(PATHS))
async def test_every_toolset_delete_path_forgets_the_sign_in(path, oauth_app, raw_client) -> None:
    delete, harness_id = PATHS[path]
    storage_provider = oauth_app.state.storage_provider
    service: ToolsetOAuthService = oauth_app.state.mcp_oauth
    toolset = _toolset("acme__kb" if harness_id else "kb", harness_id=harness_id)
    await storage_provider.get_storage(Toolset).create(toolset)
    await _connected(service, toolset)

    await delete(oauth_app, raw_client, toolset)

    assert await storage_provider.get_storage(Toolset).get(toolset.id) is None, "the path did not delete the toolset"
    assert await service.store.get_tokens(toolset.id) is None, "the tokens outlived the toolset"
    assert await service.store.get_client(toolset.id) is None, "the registered client outlived the toolset"
    assert await storage_provider.get_storage(McpOAuthPendingSignIn).get(pending_key(STATE)) is None, "a pending sign-in outlived the toolset"

    again = _toolset(toolset.id)
    await storage_provider.get_storage(Toolset).create(again)
    status = await service.status(again)
    assert (status.state, status.connected_at) == ("needs_sign_in", None), "a toolset created again came back on the old sign-in"


def _system_tools(app):
    return build_system_toolset(
        storage_provider=app.state.storage_provider, provider_registry=app.state.provider_registry, mcp_oauth=app.state.mcp_oauth,
    )


async def test_the_system_tool_forgets_the_sign_in_before_it_deletes_the_row(oauth_app, monkeypatch) -> None:
    """Review L3 of #852: as REST does, the sign-in goes first, so a delete never leaves a sign-in behind a deleted toolset."""
    storage_provider = oauth_app.state.storage_provider
    service: ToolsetOAuthService = oauth_app.state.mcp_oauth
    toolset = _toolset("kb")
    await storage_provider.get_storage(Toolset).create(toolset)
    await _connected(service, toolset)
    seen: list[bool] = []
    forget = service.forget

    async def forget_and_look(toolset_id: str) -> None:
        seen.append(await storage_provider.get_storage(Toolset).get(toolset_id) is not None)
        await forget(toolset_id)

    monkeypatch.setattr(service, "forget", forget_and_look)
    result = await _system_tools(oauth_app).call(tool_name="delete_toolset", arguments={"id": "kb"}, ctx=caller("user"))
    assert not result.is_error, result.output
    assert seen == [True], "the sign-in was forgotten after the row was gone"
    assert await storage_provider.get_storage(Toolset).get("kb") is None and await service.store.get_tokens("kb") is None


async def test_a_forget_that_fails_leaves_the_toolset_and_answers_an_error(oauth_app, monkeypatch) -> None:
    storage_provider = oauth_app.state.storage_provider
    service: ToolsetOAuthService = oauth_app.state.mcp_oauth
    toolset = _toolset("kb")
    await storage_provider.get_storage(Toolset).create(toolset)
    await _connected(service, toolset)

    async def broken(toolset_id: str) -> None:
        raise RuntimeError("storage went away")

    monkeypatch.setattr(service, "forget", broken)
    result = await _system_tools(oauth_app).call(tool_name="delete_toolset", arguments={"id": "kb"}, ctx=caller("user"))
    assert result.is_error
    error = json.loads(result.output)
    assert error["type"] == "storage-error" and "was not deleted" in error["message"] and "RuntimeError" in error["message"]
    assert await storage_provider.get_storage(Toolset).get("kb") is not None, "the toolset stays when its sign-in could not be forgotten"
    assert await service.store.get_tokens("kb") is not None
