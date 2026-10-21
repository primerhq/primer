"""The system toolset applies the same OAuth edit rule and sign-out as REST."""

from __future__ import annotations

import json

import pytest

from primer.api.registries import ProviderRegistry
from primer.common.secret_box import SecretBox
from primer.model.provider import Toolset
from primer.model.yield_ import ToolContext
from primer.toolset.oauth.service import ToolsetOAuthService
from primer.toolset.system import build_system_toolset
from tests._support.caller import caller as _ctx
from tests.conftest import _FakeStorageProvider
from tests.toolset.oauth._service_helpers import target_of, token_row


def _body(*, scopes: list[str] | None = None) -> dict:
    return {
        "id": "kb",
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {
                "url": "https://mcp.example/mcp",
                "oauth": {
                    "grant": "authorization_code",
                    "scopes": scopes or ["read"],
                    "static_client": {
                        "client_id": "client-1",
                        "client_secret": "client-secret-test-only",
                    },
                },
            },
        },
    }


@pytest.fixture
async def world():
    storage_provider = _FakeStorageProvider()
    registry = ProviderRegistry(
        storage_provider,
        llm_factory=lambda row: object(),
        embedder_factory=lambda row: object(),
        cross_encoder_factory=lambda row: object(),
        toolset_factory=lambda row: object(),
    )
    oauth = ToolsetOAuthService(
        storage_provider=storage_provider,
        secret_box=SecretBox.from_key_material("edit-rules-test-key-0123456789"),
    )
    toolset = build_system_toolset(
        storage_provider=storage_provider,
        provider_registry=registry,
        mcp_oauth=oauth,
    )
    yield toolset, storage_provider, oauth
    await oauth.aclose()


async def _call(toolset, tool_name: str, arguments: dict, ctx: ToolContext):
    return await toolset.call(tool_name=tool_name, arguments=arguments, ctx=ctx)


async def _create_and_connect(world) -> None:
    toolset, storage_provider, oauth = world
    created = await _call(toolset, "create_toolset", {"entity": _body()}, _ctx("user"))
    assert not created.is_error, created.output
    stored = await storage_provider.get_storage(Toolset).get("kb")
    await oauth.store.replace_tokens(token_row(oauth.store, target_of(stored)))


async def test_a_user_run_cannot_change_oauth_settings(world) -> None:
    toolset, _, oauth = world
    await _create_and_connect(world)

    result = await _call(
        toolset,
        "update_toolset",
        {"id": "kb", "entity": _body(scopes=["read", "write"])},
        _ctx("user"),
    )

    assert result.is_error and json.loads(result.output)["type"] == "forbidden"
    assert await oauth.store.get_tokens("kb") is not None


async def test_an_admin_run_change_signs_the_toolset_out(world) -> None:
    toolset, _, oauth = world
    await _create_and_connect(world)

    result = await _call(
        toolset,
        "update_toolset",
        {"id": "kb", "entity": _body(scopes=["read", "write"])},
        _ctx("admin"),
    )

    assert not result.is_error, result.output
    assert await oauth.store.get_tokens("kb") is None


async def test_the_update_descriptor_states_the_oauth_rule(world) -> None:
    toolset, _, _ = world
    tools = {tool.id: tool async for tool in toolset.list_tools()}

    assert "signs it out" in tools["update_toolset"].description
