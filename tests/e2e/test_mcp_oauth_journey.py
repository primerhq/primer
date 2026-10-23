"""MCP OAuth journeys against the hermetic authorization and resource server."""

from __future__ import annotations

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401
from tests._support.runs import make_scripted_agent, start_agent_session, wait_completed
from tests._support.seeding import local_workspace  # noqa: F401


pytestmark = pytest.mark.asyncio


def _oauth_toolset(toolset_id: str, mcp_url: str, *, oauth: dict | None = None) -> dict:
    return {
        "id": toolset_id,
        "provider": "mcp",
        "config": {
            "transport": "http",
            "config": {
                "url": mcp_url,
                "oauth": {"scopes": ["mcp"]} if oauth is None else oauth,
            },
        },
    }


async def _create_toolset(client, toolset_id: str, mcp_url: str, *, oauth: dict | None = None) -> None:
    response = await client.post(
        "/v1/toolsets",
        json=_oauth_toolset(toolset_id, mcp_url, oauth=oauth),
    )
    assert response.status_code == 201, response.text


async def _browser_connect(client, toolset_id: str) -> dict:
    started = await client.post(f"/v1/toolsets/{toolset_id}/oauth/start")
    assert started.status_code == 200, started.text
    authorize_url = started.json()["authorize_url"]
    assert authorize_url

    authorized = await client.get(authorize_url, follow_redirects=False)
    assert authorized.status_code == 302, authorized.text
    callback = await client.get(
        authorized.headers["location"],
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"].startswith("/console/?mcp_oauth=connected")

    status = await client.get(f"/v1/toolsets/{toolset_id}/oauth/status")
    assert status.status_code == 200, status.text
    assert status.json()["state"] == "connected"
    return status.json()


async def _run_echo(
    client,
    mock_llm,
    workspace_id: str,
    *,
    suffix: str,
    toolset_id: str,
) -> tuple[dict, list[dict]]:
    registry, mock_base_url = mock_llm
    scenario = f"scripted:oauth-{suffix}"
    tool_name = f"{toolset_id}__echo"
    agent = await make_scripted_agent(
        client,
        registry,
        mock_base_url,
        suffix=suffix,
        scenario=scenario,
        tools=[tool_name],
        rules=[
            Rule(
                when_last_user_contains="echo",
                when_tool_result=False,
                emit_tool=tool_name,
                emit_args={"text": "hi"},
            ),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    session_id = await start_agent_session(
        client,
        workspace_id=workspace_id,
        agent_id=agent["agent_id"],
        instructions="echo hi",
    )
    final = await wait_completed(client, session_id, timeout_s=90)
    requests = [request for request in registry.requests if request.get("model") == scenario]
    return final, requests


def _echoed(requests: list[dict], text: str = "hi") -> bool:
    """The echo tool's own answer reached the model: a tool result that IS the text it was asked to echo (audit G4-17). Any tool-role message is
    not enough: the listing alone would satisfy that, and a failing call answers a tool error."""
    return any(result.strip() == text for result in _tool_results(requests))


def _tool_results(requests: list[dict]) -> list[str]:
    return [
        str(message.get("content", ""))
        for request in requests
        for message in request.get("messages") or []
        if message.get("role") == "tool"
    ]


async def test_browser_sign_in_then_the_agent_uses_the_token(
    client,
    mock_llm,
    mock_oauth_mcp,
    local_workspace,
    unique_suffix,
) -> None:
    toolset_id = f"oauth-browser-{unique_suffix}"
    await _create_toolset(client, toolset_id, mock_oauth_mcp.mcp_url)
    try:
        await _browser_connect(client, toolset_id)
        final, requests = await _run_echo(
            client,
            mock_llm,
            local_workspace["workspace"],
            suffix=f"browser-{unique_suffix}",
            toolset_id=toolset_id,
        )

        assert final.get("session_state") == "parked", final
        assert final.get("last_turn_error") is None, final
        assert _echoed(requests), f"the echo call's output never reached the model: {_tool_results(requests)}"
        assert mock_oauth_mcp.state.seen_tokens
        assert all(token.startswith("mock-at-") for token in mock_oauth_mcp.state.seen_tokens)
    finally:
        await client.delete(f"/v1/toolsets/{toolset_id}")


async def test_a_lost_sign_in_is_a_tool_error_and_the_session_completes(
    client,
    mock_llm,
    mock_oauth_mcp,
    local_workspace,
    unique_suffix,
) -> None:
    toolset_id = f"oauth-lost-{unique_suffix}"
    await _create_toolset(client, toolset_id, mock_oauth_mcp.mcp_url)
    try:
        await _browser_connect(client, toolset_id)
        mock_oauth_mcp.state.revoke_all()

        final, requests = await _run_echo(
            client,
            mock_llm,
            local_workspace["workspace"],
            suffix=f"lost-{unique_suffix}",
            toolset_id=toolset_id,
        )
        expected = (
            f"toolset {toolset_id} is unavailable this turn: "
            f"the MCP server {toolset_id} needs an operator to reconnect it"
        )

        assert final.get("session_state") == "parked", final
        assert final.get("last_turn_error") is None, final
        assert any(expected in result for result in _tool_results(requests)), requests
        status = await client.get(f"/v1/toolsets/{toolset_id}/oauth/status")
        assert status.status_code == 200, status.text
        assert status.json()["state"] == "needs_sign_in"
    finally:
        await client.delete(f"/v1/toolsets/{toolset_id}")


async def test_an_unreachable_mcp_toolset_does_not_end_the_turn(
    client,
    mock_llm,
    local_workspace,
    unique_suffix,
) -> None:
    toolset_id = f"unreachable-{unique_suffix}"
    response = await client.post(
        "/v1/toolsets?allow_unreachable=true",
        json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "http",
                "config": {"url": "http://127.0.0.1:1/mcp"},
            },
        },
    )
    assert response.status_code == 201, response.text
    try:
        registry, mock_base_url = mock_llm
        scenario = f"scripted:unreachable-{unique_suffix}"
        agent = await make_scripted_agent(
            client,
            registry,
            mock_base_url,
            suffix=f"unreachable-{unique_suffix}",
            scenario=scenario,
            tools=[f"{toolset_id}__echo", "misc__uuid_v4"],
            rules=[
                Rule(
                    when_tool_offered="uuid_v4",
                    when_tool_result=False,
                    emit_tool="misc__uuid_v4",
                ),
                Rule(when_tool_result=True, emit_text="done"),
            ],
        )
        session_id = await start_agent_session(
            client,
            workspace_id=local_workspace["workspace"],
            agent_id=agent["agent_id"],
            instructions="use the working tool",
        )
        final = await wait_completed(client, session_id, timeout_s=90)

        assert final.get("session_state") == "parked", final
        assert final.get("last_turn_error") is None, final
        requests = [request for request in registry.requests if request.get("model") == scenario]
        assert _tool_results(requests), "the working tool did not complete beside the unreachable toolset"
    finally:
        await client.delete(f"/v1/toolsets/{toolset_id}")


async def test_disconnect_revokes(client, mock_oauth_mcp, unique_suffix) -> None:
    toolset_id = f"oauth-disconnect-{unique_suffix}"
    await _create_toolset(client, toolset_id, mock_oauth_mcp.mcp_url)
    try:
        await _browser_connect(client, toolset_id)
        disconnected = await client.post(f"/v1/toolsets/{toolset_id}/oauth/disconnect")

        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["state"] == "needs_sign_in"
        assert len(mock_oauth_mcp.state.revocations) == 1
        assert mock_oauth_mcp.state.revocations[0].startswith("mock-rt-")
        status = await client.get(f"/v1/toolsets/{toolset_id}/oauth/status")
        assert status.status_code == 200, status.text
        assert status.json()["state"] == "needs_sign_in"
    finally:
        await client.delete(f"/v1/toolsets/{toolset_id}")


async def test_client_credentials_connect(
    client,
    mock_llm,
    mock_oauth_mcp,
    local_workspace,
    unique_suffix,
) -> None:
    toolset_id = f"oauth-cc-{unique_suffix}"
    await _create_toolset(
        client,
        toolset_id,
        mock_oauth_mcp.mcp_url,
        oauth={
            "grant": "client_credentials",
            "scopes": ["mcp"],
            "resource_uri": mock_oauth_mcp.mcp_url,
            "token_endpoint": mock_oauth_mcp.issuer + "/token",
            "static_client": {
                "client_id": "cc-client",
                "client_secret": "cc-secret",
            },
        },
    )
    try:
        connected = await client.post(f"/v1/toolsets/{toolset_id}/oauth/start")
        assert connected.status_code == 200, connected.text
        assert connected.json()["authorize_url"] is None
        assert connected.json()["status"]["state"] == "connected"

        final, requests = await _run_echo(
            client,
            mock_llm,
            local_workspace["workspace"],
            suffix=f"cc-{unique_suffix}",
            toolset_id=toolset_id,
        )
        assert final.get("session_state") == "parked", final
        assert final.get("last_turn_error") is None, final
        assert _echoed(requests), f"the echo call's output never reached the model: {_tool_results(requests)}"
        assert mock_oauth_mcp.state.seen_tokens
        assert mock_oauth_mcp.state.token_requests[-1]["grant_type"] == "client_credentials"
    finally:
        await client.delete(f"/v1/toolsets/{toolset_id}")


async def test_a_user_cannot_connect(client, base_url, mock_oauth_mcp, unique_suffix) -> None:
    toolset_id = f"oauth-user-{unique_suffix}"
    username = f"oauth-user-{unique_suffix}"
    password = "oauth-user-password-123"
    await _create_toolset(client, toolset_id, mock_oauth_mcp.mcp_url)
    made = await client.post(
        "/v1/admin/users",
        json={"username": username, "password": password, "role": "user"},
    )
    assert made.status_code == 201, made.text
    user_id = made.json()["id"]
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
        ) as user_client:
            login = await user_client.post(
                "/v1/auth/login",
                json={"username": username, "password": password},
            )
            assert login.status_code == 200, login.text
            for action in ("start", "disconnect"):
                refused = await user_client.post(
                    f"/v1/toolsets/{toolset_id}/oauth/{action}"
                )
                assert refused.status_code == 403, refused.text
                assert refused.json()["extensions"]["error"] == "forbidden_role"
                assert "admin role" in refused.text
    finally:
        await client.delete(f"/v1/toolsets/{toolset_id}")
        await client.delete(f"/v1/admin/users/{user_id}")
