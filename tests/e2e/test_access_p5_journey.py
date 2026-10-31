"""A browser MCP sign-in belongs to one person, over the live API (access control, Phase 5).

Against the hermetic OAuth + MCP mock (``tests/_support/mock_oauth_mcp.py``) and the scripted LLM:

1. A (the e2e admin) creates an OAuth toolset T and a public agent that calls ``T__echo``, and shares T read with B.
2. A connects; A's run calls ``T__echo`` and the mock sees A's token.
3. B runs the same agent: no token is sent and the tool answers that the person the run acts for must sign in. B connects
   for themselves; B's run succeeds and the mock sees a token that is not A's.
4. B disconnects; A's run still succeeds on A's own sign-in.
5. C (T is not shared with them) connects an MCP client: ``tools/list`` has none of T's tools and ``tools/call T__echo`` is
   refused like an unknown tool.
6. The ``toolset.updated`` event of A's sharing change names A (``user:<A id>``), not ``system``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from tests._support.mock_llm import Rule
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401
from tests._support.model_profiles import agent_model
from tests._support.runs import make_scripted_agent, start_agent_session, wait_completed
from tests._support.seeding import local_workspace  # noqa: F401
from tests.e2e.test_mcp_oauth_journey import _browser_connect, _create_toolset, _echoed, _tool_results

pytestmark = pytest.mark.asyncio

_PASSWORD = "access-p5-password-123"
SIGN_IN = "needs the person this run acts for to sign in"


@asynccontextmanager
async def _user(client: httpx.AsyncClient, base_url: str, username: str) -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    made = await client.post("/v1/admin/users", json={"username": username, "password": _PASSWORD, "role": "user"})
    assert made.status_code == 201, made.text
    user_id = made.json()["id"]
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0)) as c:
            login = await c.post("/v1/auth/login", json={"username": username, "password": _PASSWORD})
            assert login.status_code == 200, login.text
            yield user_id, c
    finally:
        await client.delete(f"/v1/admin/users/{user_id}")


async def _share(client: httpx.AsyncClient, plural: str, entity_id: str, **change) -> None:
    """Rewrite one row's sharing from its current view (owners kept unless ``owners`` is given; ``use_only`` rows have none)."""
    view = (await client.get(f"/v1/{plural}/{entity_id}/access")).json()
    body = {
        "owners": [o["id"] for o in view["owners"]],
        "grants": {g["user"]["id"]: g["level"] for g in view["grants"]},
        "public": view["public"],
        "public_tools": view["public_tools"],
        "rev": view["rev"],
    }
    body.update(change)
    put = await client.put(f"/v1/{plural}/{entity_id}/access", json=body)
    assert put.status_code == 200, put.text


async def _run(starter: httpx.AsyncClient, watcher: httpx.AsyncClient, registry, mock_oauth, *, workspace_id: str, agent: dict):
    """Start the agent as ``starter``, wait for the turn (read as the admin ``watcher``); return (final, the run's model requests, the
    tokens the MCP server saw during it)."""
    seen, asked = len(mock_oauth.state.seen_tokens), len(registry.requests)
    sid = await start_agent_session(starter, workspace_id=workspace_id, agent_id=agent["agent_id"], instructions="echo hi")
    final = await wait_completed(watcher, sid, timeout_s=90)
    requests = [r for r in registry.requests[asked:] if r.get("model") == agent["model"]]
    return final, requests, set(mock_oauth.state.seen_tokens[seen:])


async def test_each_person_signs_in_for_themselves(client, base_url, mock_llm, mock_oauth_mcp, local_workspace, unique_suffix) -> None:
    registry, mock_base_url = mock_llm
    toolset_id = f"p5-oauth-{unique_suffix}"
    wid = local_workspace["workspace"]
    a_id = (await client.get("/v1/auth/status")).json()["principal"]["user_id"]
    await _create_toolset(client, toolset_id, mock_oauth_mcp.mcp_url)
    agent = await make_scripted_agent(
        client, registry, mock_base_url,
        suffix=f"p5-{unique_suffix}", scenario=f"scripted:p5-{unique_suffix}", tools=[f"{toolset_id}__echo"],
        rules=[
            Rule(when_last_user_contains="echo", when_tool_result=False, emit_tool=f"{toolset_id}__echo", emit_args={"text": "hi"}),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    try:
        async with (
            _user(client, base_url, f"p5-b-{unique_suffix}") as (b_id, b),
            _user(client, base_url, f"p5-c-{unique_suffix}") as (_c_id, c),
        ):
            # 1. The agent is public, its model is usable by everyone, B may work in A's workspace, and T is shared read with B.
            await _share(client, "agents", agent["agent_id"], public=True)
            await _share(client, "model_profiles", agent_model(agent["provider_id"], agent["model"])["profile_id"], public=True)
            await _share(client, "llm_providers", agent["provider_id"], public=True)
            await _share(client, "workspace_providers", local_workspace["provider"], public=True)
            await _share(client, "workspace_templates", local_workspace["template"], public=True)
            await _share(client, "workspaces", wid, grants={b_id: "full"})
            await _share(client, "toolsets", toolset_id, grants={b_id: "read"})

            # 6. A's sharing change (the last write of T) is attributed to A, never to "system".
            events = await client.get("/v1/events", params={"event_type": "toolset.updated", "entity_id": toolset_id, "limit": 500})
            assert events.status_code == 200, events.text
            assert events.json()["items"], "the sharing change wrote no toolset.updated event"
            shared = max(events.json()["items"], key=lambda e: e["id"])
            assert shared["actor"] == f"user:{a_id}", shared
            assert b_id in shared["payload"]["access"]["grants"], "the newest toolset.updated event is the sharing change"

            # 2. A connects; A's run sends A's token.
            await _browser_connect(client, toolset_id)
            final, requests, a_tokens = await _run(client, client, registry, mock_oauth_mcp, workspace_id=wid, agent=agent)
            assert final.get("last_turn_error") is None, final
            assert _echoed(requests), _tool_results(requests)
            assert a_tokens and all(t.startswith("mock-at-") for t in a_tokens)

            # 3. B's run: nobody's token is sent, and the tool says B must sign in. Then B connects and B's own token is sent.
            final, requests, sent = await _run(b, client, registry, mock_oauth_mcp, workspace_id=wid, agent=agent)
            assert final.get("last_turn_error") is None, final
            assert sent == set(), "B's run sent somebody else's token"
            assert any(SIGN_IN in result for result in _tool_results(requests)), _tool_results(requests)
            status = (await b.get(f"/v1/toolsets/{toolset_id}/oauth/status")).json()
            assert (status["state"], status["per_user"]) == ("needs_sign_in", True)

            await _browser_connect(b, toolset_id)
            final, requests, b_tokens = await _run(b, client, registry, mock_oauth_mcp, workspace_id=wid, agent=agent)
            assert final.get("last_turn_error") is None, final
            assert _echoed(requests), _tool_results(requests)
            assert b_tokens and not (b_tokens & a_tokens), "B's run must send B's own token"

            # 4. B disconnects; A's sign-in is untouched.
            disconnected = await b.post(f"/v1/toolsets/{toolset_id}/oauth/disconnect")
            assert disconnected.status_code == 200 and disconnected.json()["state"] == "needs_sign_in", disconnected.text
            assert (await client.get(f"/v1/toolsets/{toolset_id}/oauth/status")).json()["state"] == "connected"
            final, requests, sent = await _run(client, client, registry, mock_oauth_mcp, workspace_id=wid, agent=agent)
            assert final.get("last_turn_error") is None, final
            assert _echoed(requests), _tool_results(requests)
            assert sent and not (sent & b_tokens)

            # 5. C's MCP client sees none of T's tools and cannot call one.
            await _c_mcp_client_sees_no_tool_of(client, c, base_url, toolset_id)
    finally:
        await client.put("/v1/mcp_exposure", json={"enabled": False, "allowed_tools": []})
        await client.delete(f"/v1/toolsets/{toolset_id}")


async def _c_mcp_client_sees_no_tool_of(admin: httpx.AsyncClient, c: httpx.AsyncClient, base_url: str, toolset_id: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    # An owned toolset cannot even be allowlisted (only built-in tools are exposable); expose a harmless built-in one.
    refused = await admin.put("/v1/mcp_exposure", json={"enabled": True, "allowed_tools": [f"{toolset_id}__echo"]})
    assert refused.status_code == 422, refused.text
    enabled = await admin.put("/v1/mcp_exposure", json={"enabled": True, "allowed_tools": ["misc__uuid_v4"]})
    assert enabled.status_code in (200, 204), enabled.text
    token = await c.post("/v1/auth/tokens", json={"name": "p5-c-mcp", "scopes": ["mcp"]})
    assert token.status_code == 201, token.text
    headers = {"Authorization": f"Bearer {token.json()['plaintext']}"}

    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(f"{base_url}/v1/mcp/", http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == {"misc__uuid_v4"}
                assert not any(name.startswith(f"{toolset_id}__") for name in names)
                try:
                    result = await session.call_tool(f"{toolset_id}__echo", arguments={"text": "hi"})
                except Exception as exc:  # noqa: BLE001 - the SDK raises the JSON-RPC method-not-found error
                    assert "not exposed" in str(exc).lower() or "method" in str(exc).lower(), repr(exc)
                else:
                    assert result.is_error, "a tool of an owned toolset was called over MCP"
