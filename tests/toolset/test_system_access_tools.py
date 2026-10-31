"""The sharing tools of the system toolset (access-control plan, Phase 1 Task 8, part A).

They call the same service the REST routes call; these tests pin the tool-side refusals and the two ways a tool learns
its caller (a run's ToolContext, or the MCP endpoint's current actor).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from primer.mcp.server import current_actor
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.user import User
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset._system_access import access_tools


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.fixture
async def tools(provider: SqliteStorageProvider) -> dict:
    users = provider.get_storage(User)
    for uid, name in (("u1", "alice"), ("u2", "bob"), ("u3", "carol")):
        await users.create(User(id=uid, username=name, password_hash=None, created_at=datetime.now(UTC), role="user"))
    agent = Agent.model_validate({"id": "a1", "description": "d", "model": {"profile_id": "p"}, "access": Access(owners=["u1"]).model_dump()})
    await provider.get_storage(Agent).create(agent)
    return access_tools(storage_provider=provider)


def _ctx(uid: str, role: str = "user") -> ToolContext:
    ref = PrincipalRef(type="user", id=uid, display=uid, role=role, source="local", user_id=uid)
    return ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=ref)


async def _call(tools: dict, name: str, args: dict, ctx: ToolContext | None):
    _tool, handler = tools[name]
    result = await handler(args, ctx=ctx)
    return result.is_error, json.loads(result.output)


async def test_owner_reads_and_shares_through_the_tools(tools):
    is_err, view = await _call(tools, "get_entity_access", {"kind": "agent", "id": "a1"}, _ctx("u1"))
    assert not is_err and view["your_level"] == "owner" and view["rev"] == 0
    is_err, view = await _call(
        tools, "set_entity_access", {"kind": "agent", "id": "a1", "owners": ["u1"], "grants": {"u2": "read"}, "rev": 0}, _ctx("u1")
    )
    assert not is_err and [(g["user"]["username"], g["level"]) for g in view["grants"]] == [("bob", "read")]


async def test_unreadable_entity_answers_like_a_missing_one(tools):
    hidden = await _call(tools, "get_entity_access", {"kind": "agent", "id": "a1"}, _ctx("u3"))
    missing = await _call(tools, "get_entity_access", {"kind": "agent", "id": "nope"}, _ctx("u3"))
    assert hidden[0] and missing[0]
    assert hidden[1]["type"] == missing[1]["type"] == "not-found"


async def test_a_non_owner_cannot_change_sharing(tools):
    await _call(tools, "set_entity_access", {"kind": "agent", "id": "a1", "owners": ["u1"], "grants": {"u2": "full"}}, _ctx("u1"))
    is_err, body = await _call(tools, "set_entity_access", {"kind": "agent", "id": "a1", "owners": ["u2"]}, _ctx("u2"))
    assert is_err and body["type"] == "forbidden"


async def test_stale_rev_and_bad_input_are_typed(tools):
    stale = await _call(tools, "set_entity_access", {"kind": "agent", "id": "a1", "owners": ["u1"], "rev": 5}, _ctx("u1"))
    empty = await _call(tools, "set_entity_access", {"kind": "agent", "id": "a1", "owners": []}, _ctx("u1"))
    tools_on_agent = await _call(
        tools, "set_entity_access", {"kind": "agent", "id": "a1", "owners": ["u1"], "public_tools": ["x"]}, _ctx("u1")
    )
    assert stale[1]["type"] == "conflict"
    assert empty[1]["type"] == "validation-error"
    assert tools_on_agent[1]["type"] == "validation-error"


async def test_mcp_caller_is_the_request_actor(tools):
    token = current_actor.set(Principal(type="api_token", id="tok", display="t", role="user", source="local", user_id="u1"))
    try:
        is_err, view = await _call(tools, "get_entity_access", {"kind": "agent", "id": "a1"}, None)
    finally:
        current_actor.reset(token)
    assert not is_err and view["your_level"] == "owner"


async def test_no_known_caller_sees_only_public_rows(tools):
    is_err, body = await _call(tools, "get_entity_access", {"kind": "agent", "id": "a1"}, None)
    assert is_err and body["type"] == "not-found"


async def test_lookup_users_through_the_tool(tools):
    is_err, found = await _call(tools, "lookup_users", {"q": "b"}, _ctx("u1"))
    assert not is_err and found == [{"id": "u2", "username": "bob"}]


async def test_a_workspace_is_shared_through_the_tools(tools, provider):
    """Workspaces are an owned kind for the tools too (P2 gap 3): the same rules as ``PUT /v1/workspaces/{id}/access``."""
    from pydantic import SecretStr

    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta

    await provider.get_storage(Workspace).create(Workspace(
        id="ws-a", template_id="tpl", provider_id="local", created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://x", token=SecretStr("t")), access=Access(owners=["u1"]),
    ))
    is_err, view = await _call(tools, "get_entity_access", {"kind": "workspace", "id": "ws-a"}, _ctx("u1"))
    assert not is_err and view["your_level"] == "owner", view
    is_err, view = await _call(
        tools, "set_entity_access", {"kind": "workspace", "id": "ws-a", "owners": ["u1"], "grants": {"u2": "full"}, "rev": 0}, _ctx("u1")
    )
    assert not is_err and [(g["user"]["username"], g["level"]) for g in view["grants"]] == [("bob", "full")], view
    is_err, view = await _call(tools, "get_entity_access", {"kind": "workspace", "id": "ws-a"}, _ctx("u2"))
    assert not is_err and view["your_level"] == "full"
    full_cannot_share = await _call(tools, "set_entity_access", {"kind": "workspace", "id": "ws-a", "owners": ["u2"]}, _ctx("u2"))
    assert full_cannot_share[0] and full_cannot_share[1]["type"] == "forbidden"
    no_public_tools = await _call(
        tools, "set_entity_access", {"kind": "workspace", "id": "ws-a", "owners": ["u1"], "public_tools": ["x"]}, _ctx("u1")
    )
    assert no_public_tools[0] and no_public_tools[1]["type"] == "validation-error"
    stranger = await _call(tools, "get_entity_access", {"kind": "workspace", "id": "ws-a"}, _ctx("u3"))
    assert stranger[0] and stranger[1]["type"] == "not-found"
