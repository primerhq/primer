"""The system status tool reports the shared MCP sign-in without exposing credentials."""

from __future__ import annotations

import json

import pytest

from primer.api.registries import ProviderRegistry
from primer.common.secret_box import SecretBox
from primer.model.mcp_oauth import McpSignInRequiredError
from primer.model.provider import Toolset
from primer.toolset.oauth.service import ToolsetOAuthService
from primer.toolset.system import build_system_toolset
from tests.conftest import _FakeStorageProvider
from tests.toolset.oauth._service_helpers import make_toolset, target_of, token_row


URL = "https://mcp.example/mcp"


def _registry(storage_provider, *, toolset_factory=lambda row: object()) -> ProviderRegistry:
    return ProviderRegistry(
        storage_provider,
        llm_factory=lambda row: object(),
        embedder_factory=lambda row: object(),
        cross_encoder_factory=lambda row: object(),
        toolset_factory=toolset_factory,
    )


@pytest.fixture
async def world():
    storage_provider = _FakeStorageProvider()
    registry = _registry(storage_provider)
    oauth = ToolsetOAuthService(
        storage_provider=storage_provider,
        secret_box=SecretBox.from_key_material("status-tool-test-key-0123456789"),
    )
    system = build_system_toolset(
        storage_provider=storage_provider,
        provider_registry=registry,
        mcp_oauth=oauth,
    )
    registry._system_toolset_provider = system  # noqa: SLF001
    yield system, storage_provider, oauth
    await oauth.aclose()


async def _call(system, tool_name: str, arguments: dict):
    result = await system.call(tool_name=tool_name, arguments=arguments)
    return result, json.loads(result.output)


async def _store(storage_provider, row: Toolset) -> None:
    await storage_provider.get_storage(Toolset).create(row)


async def test_the_status_of_an_unconnected_oauth_toolset(world) -> None:
    system, storage_provider, _ = world
    await _store(storage_provider, make_toolset(URL, oauth={}))

    result, answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "kb"})

    assert not result.is_error
    assert answer == {
        "toolset_id": "kb",
        "mode": "oauth_browser",
        "state": "needs_sign_in",
        "reason": "Not connected.",
        "expires_at": None,
        "connected_at": None,
        "refreshed_at": None,
        "scope": None,
        "redirect_uri": None,
    }


async def test_a_header_toolset_is_not_applicable(world) -> None:
    system, storage_provider, _ = world
    await _store(storage_provider, make_toolset(URL, oauth=None, headers={"Authorization": "Bearer test-only"}))

    result, answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "kb"})

    assert not result.is_error
    assert answer["mode"] == "header" and answer["state"] == "not_applicable"


async def test_an_unknown_toolset_is_not_found(world) -> None:
    system, _, _ = world

    result, answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "missing"})

    assert result.is_error and answer["type"] == "not-found"


async def test_a_built_in_toolset_is_not_applicable(world) -> None:
    system, _, _ = world

    result, answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "system"})

    assert not result.is_error
    assert answer["mode"] == "none" and answer["state"] == "not_applicable"


async def test_without_the_service_the_tool_is_unavailable() -> None:
    storage_provider = _FakeStorageProvider()
    registry = _registry(storage_provider)
    await _store(storage_provider, make_toolset(URL, oauth={}))
    system = build_system_toolset(storage_provider=storage_provider, provider_registry=registry)

    result, answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "kb"})

    assert result.is_error and answer["type"] == "unavailable"


async def test_the_answer_never_holds_a_token(world) -> None:
    system, storage_provider, oauth = world
    row = make_toolset(URL, oauth={})
    await _store(storage_provider, row)
    await oauth.store.replace_tokens(token_row(oauth.store, target_of(row)))

    result, answer = await _call(system, "get_toolset_auth_status", {"toolset_id": "kb"})

    assert not result.is_error and answer["state"] == "connected"
    assert "at-plain" not in result.output and "rt-plain" not in result.output
    assert "access_token" not in result.output and "refresh_token" not in result.output


class _NeedsSignInProvider:
    async def list_tools(self, *, principal=None):
        del principal
        raise McpSignInRequiredError("kb", reason="Not connected.")
        yield  # pragma: no cover


async def test_list_toolset_tools_names_the_sign_in() -> None:
    storage_provider = _FakeStorageProvider()
    registry = _registry(storage_provider, toolset_factory=lambda row: _NeedsSignInProvider())
    await _store(storage_provider, make_toolset(URL, oauth={}))
    system = build_system_toolset(storage_provider=storage_provider, provider_registry=registry)

    result, answer = await _call(system, "list_toolset_tools", {"toolset_id": "kb"})

    assert result.is_error and answer["type"] == "sign-in-required"
    assert answer["message"] == "the MCP server kb needs an operator to reconnect it"


async def test_the_descriptor(world) -> None:
    system, _, _ = world
    tools = {tool.id: tool async for tool in system.list_tools()}
    descriptor = tools["get_toolset_auth_status"]

    assert descriptor.required_role == "user"
    assert "Read-only" in descriptor.description
