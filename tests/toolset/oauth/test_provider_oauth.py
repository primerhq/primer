from __future__ import annotations

import pytest

from primer.common.secret_box import SecretBox
from primer.model.except_ import ProviderError
from primer.model.mcp_oauth import McpSignInRequiredError
from primer.toolset.mcp import McpToolsetProvider
from primer.toolset.oauth.service import TOKEN_REJECTED, ToolsetOAuthService
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401
from tests.toolset.oauth._service_helpers import connect, make_toolset, target_of


async def _service(fake_storage_provider) -> ToolsetOAuthService:
    return ToolsetOAuthService(
        storage_provider=fake_storage_provider,
        secret_box=SecretBox.from_key_material("provider-test-key-0123456789"),
    )


async def test_tools_list_with_a_connected_toolset(fake_storage_provider, mock_oauth_mcp) -> None:
    service = await _service(fake_storage_provider)
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    await connect(service, toolset)
    provider = McpToolsetProvider(toolset.id, toolset.config, oauth=service)
    try:
        tools = [tool.id async for tool in provider.list_tools()]
        assert sorted(tools) == ["bump", "echo"]
        assert mock_oauth_mcp.state.seen_tokens
    finally:
        await service.aclose()


async def test_an_unconnected_oauth_toolset_raises_sign_in_required(fake_storage_provider, mock_oauth_mcp) -> None:
    service = await _service(fake_storage_provider)
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    provider = McpToolsetProvider(toolset.id, toolset.config, oauth=service)
    try:
        with pytest.raises(McpSignInRequiredError):
            _ = [tool async for tool in provider.list_tools()]
        assert mock_oauth_mcp.state.seen_tokens == []
    finally:
        await service.aclose()


async def test_a_revoked_token_signs_the_toolset_out_during_a_call(fake_storage_provider, mock_oauth_mcp) -> None:
    service = await _service(fake_storage_provider)
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    await connect(service, toolset)
    mock_oauth_mcp.state.revoke_all()
    provider = McpToolsetProvider(toolset.id, toolset.config, oauth=service)
    try:
        with pytest.raises(McpSignInRequiredError):
            await provider.call(tool_name="echo", arguments={"text": "hello"})
    finally:
        await service.aclose()


async def test_an_expired_access_token_is_refreshed_on_401(fake_storage_provider, mock_oauth_mcp) -> None:
    service = await _service(fake_storage_provider)
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    await connect(service, toolset)
    mock_oauth_mcp.state.expire_access_tokens()
    provider = McpToolsetProvider(toolset.id, toolset.config, oauth=service)
    try:
        result = await provider.call(tool_name="echo", arguments={"text": "hello"})
        assert result.output == "hello"
        assert any(request["grant_type"] == "refresh_token" for request in mock_oauth_mcp.state.token_requests)
    finally:
        await service.aclose()


async def test_a_server_that_rejects_the_refreshed_token_too_signs_the_toolset_out(fake_storage_provider, mock_oauth_mcp) -> None:
    """Audit G4-11: the MCP server answers 401, the refresh succeeds, and the server rejects the NEW token as well. The toolset is signed out with
    TOKEN_REJECTED, so its status stops saying connected, and the next call fails at once instead of spending another refresh token."""
    service = await _service(fake_storage_provider)
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={})
    await connect(service, toolset)
    mock_oauth_mcp.state.reject_bearer_tokens = True
    provider = McpToolsetProvider(toolset.id, toolset.config, oauth=service)
    try:
        with pytest.raises(McpSignInRequiredError):
            await provider.call(tool_name="echo", arguments={"text": "hello"})
        refreshes = [request["grant_type"] for request in mock_oauth_mcp.state.token_requests].count("refresh_token")
        assert refreshes == 1
        status = await service.status(toolset)
        assert (status.state, status.reason) == ("needs_sign_in", TOKEN_REJECTED)
        row = await service.store.get_tokens(toolset.id)
        assert (row.state, row.access_token_enc, row.refresh_token_enc) == ("needs_sign_in", None, None)

        presented = len(mock_oauth_mcp.state.seen_tokens)
        with pytest.raises(McpSignInRequiredError) as caught:
            await provider.call(tool_name="echo", arguments={"text": "hello"})
        assert caught.value.reason == TOKEN_REJECTED
        assert [request["grant_type"] for request in mock_oauth_mcp.state.token_requests].count("refresh_token") == refreshes
        assert len(mock_oauth_mcp.state.seen_tokens) == presented, "a signed-out toolset sent a token"
    finally:
        await service.aclose()


async def test_a_client_credentials_toolset_whose_fresh_token_is_rejected_recovers_without_a_person(fake_storage_provider, mock_oauth_mcp) -> None:
    """Review L1 of #852: for client credentials a double 401 marks the row ``error`` and drops only the access token (the refresh path's rule for a
    refused client-credentials exchange), so the next call exchanges the credentials again and the toolset recovers with nobody reconnecting it."""
    service = await _service(fake_storage_provider)
    toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={"grant": "client_credentials", "static_client": {"client_id": "cc-client",
                                                                                                         "client_secret": "cc-secret"}})
    await service.connect_client_credentials(target_of(toolset), connected_by="admin")
    mock_oauth_mcp.state.reject_bearer_tokens = True
    provider = McpToolsetProvider(toolset.id, toolset.config, oauth=service)
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.call(tool_name="echo", arguments={"text": "hello"})
        assert not isinstance(caught.value, McpSignInRequiredError), "nobody needs to reconnect a client-credentials toolset"
        status = await service.status(toolset)
        assert (status.state, status.reason) == ("error", TOKEN_REJECTED)
        row = await service.store.get_tokens(toolset.id)
        assert (row.state, row.access_token_enc) == ("error", None)

        mock_oauth_mcp.state.reject_bearer_tokens = False
        exchanges = [request["grant_type"] for request in mock_oauth_mcp.state.token_requests].count("client_credentials")
        result = await provider.call(tool_name="echo", arguments={"text": "hello"})
        assert result.output == "hello"
        assert [request["grant_type"] for request in mock_oauth_mcp.state.token_requests].count("client_credentials") == exchanges + 1
        assert (await service.status(toolset)).state == "connected"
    finally:
        await service.aclose()


async def test_header_and_none_modes_never_touch_the_service(monkeypatch, mock_oauth_mcp) -> None:
    class _NoCalls:
        async def access_token(self, target):
            raise AssertionError("OAuth service was called")

    for oauth in (None,):
        toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth=oauth)
        provider = McpToolsetProvider(toolset.id, toolset.config, oauth=_NoCalls())
        with pytest.raises(Exception):
            _ = [tool async for tool in provider.list_tools()]
