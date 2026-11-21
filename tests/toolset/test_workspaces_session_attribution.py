"""MCP ``create_workspace_session`` attribution — Layer 3 Task 2 (§6.6, §8.1).

The ``create_workspace_session`` tool is a ctx-taking handler:
``InternalToolsetProvider`` injects the enclosing ``ToolContext`` when the
handler declares it, and the handler stamps
``ctx.initiated_by`` onto the created session so a sub-session an agent
spawns inherits the enclosing run's attribution. A caller with no threaded
identity (the MCP endpoint hands handlers no ``ToolContext``) gets the
fail-closed ``PrincipalRef.unattributed()`` (an ordinary ``user`` rank),
never the system principal, which clears every role floor (security
review A-20).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.mcp.server import current_actor
from primer.model.access import Access
from primer.model.principal import Principal, PrincipalRef
from primer.model.workspace import (
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
)
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import build_workspaces_toolset


# ===========================================================================
# In-memory fakes (minimal subset copied from tests/toolset/test_workspaces.py)
# ===========================================================================


class _LiveWorkspace:
    def __init__(self, workspace_id="ws-stub") -> None:
        from pydantic import SecretStr

        from primer.model.workspace import WorkspaceRuntimeMeta

        self.id = workspace_id
        self._sessions: dict[str, Any] = {}
        self.runtime_meta = WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/",
            token=SecretStr("t"),
        )

    async def get_session(self, session_id):
        return self._sessions.get(session_id)

    async def start_session(
        self, binding, *, id, instructions=None, parent_session_id=None, name=None
    ):
        self._sessions[id] = object()
        return self._sessions[id]

    async def aclose(self):
        return


class _StubBackend:
    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, Any] = {}

    async def initialize(self):
        return

    async def aclose(self):
        return

    async def create(
        self, template, *, overrides=None, workspace_id=None, resolvers=None
    ):
        ws = _LiveWorkspace(workspace_id or "ws-stub")
        self._workspaces[ws.id] = ws
        return ws

    async def get(self, workspace_id, *, template=None):
        return self._workspaces.get(workspace_id)


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, sid: str) -> None:
        self.enqueued.append(sid)


class _FakeClaimEngine:
    async def upsert(self, kind, entity_id, *, priority=100, next_attempt_at=None):
        return

    async def delete_lease(self, kind, entity_id):
        return


def _seed_agent(sp, agent_id="code-reviewer") -> None:
    from primer.model.agent import Agent, AgentModel

    sp.get_storage(Agent)._data[agent_id] = Agent(access=Access(public=True), 
        id=agent_id,
        description="x",
        model=AgentModel(profile_id="p--m"),
    )


def _provider() -> WorkspaceProvider:
    return WorkspaceProvider(access=Access(public=True), 
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/tmp/x"),
    )


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def _actor():
    """No calling session in this file: the calls act as u-1's MCP token user, whom ``create_workspace`` stamps owner
    of ``ws-stub`` (access-control P2 T6)."""
    token = current_actor.set(Principal(type="user", id="tok-u1", display="u1", role="user", source="local", user_id="u-1"))
    try:
        yield
    finally:
        current_actor.reset(token)


@pytest.fixture
def sp(fake_storage_provider):
    return fake_storage_provider


@pytest.fixture
def workspace_registry(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_StubBackend)


@pytest.fixture
def session_toolset(sp, workspace_registry):
    return build_workspaces_toolset(
        storage_provider=sp,
        workspace_registry=workspace_registry,
        scheduler=_FakeScheduler(),
        claim_engine=_FakeClaimEngine(),
        event_bus=None,
    )


@pytest.fixture
async def seeded(session_toolset, sp):
    """Seed provider + template + workspace + agent; return the workspace id."""
    # Straight to storage, public: a provider created through the tool is private (access-control P4 Task 5), and the template below is
    # saved by a plain user who must be able to use it.
    await sp.get_storage(WorkspaceProvider).create(_provider())
    await session_toolset.call(
        tool_name="create_workspace_template",
        arguments={
            "entity": {"id": "tpl-1", "description": "dev", "provider_id": "local-1"},
        },
    )
    from primer.model.principal import PrincipalRef
    from primer.model.yield_ import ToolContext

    # The template was created with no identity (admin-managed): the seed creates the workspace as a session-less admin.
    admin = ToolContext(
        tool_call_id="seed", session_id=None, workspace_id=None,
        initiated_by=PrincipalRef(type="user", id="admin-1", display="admin", role="admin", source="local", user_id="admin-1"),
    )
    create = await session_toolset.call(
        tool_name="create_workspace",
        arguments={"id": "ws-stub", "template_id": "tpl-1"},
        ctx=admin,
    )
    assert not create.is_error, create.output
    # The admin owns what it created: the users these tests act as (u-1, and u-9's token) are shared it at full, the level
    # a session start needs.
    row = await sp.get_storage(Workspace).get("ws-stub")
    row.access = Access(owners=["admin-1"], grants={"u-1": "full", "u-9": "full"})
    await sp.get_storage(Workspace).update(row)
    _seed_agent(sp)
    return "ws-stub"


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_session_stamps_ctx_initiated_by(
    sp, session_toolset,
    seeded,
) -> None:
    """A ctx carrying a per-call identity stamps the child row from it."""
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="sess-parent", workspace_id=seeded, status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        binding=AgentSessionBinding(agent_id="code-reviewer"),
    ))
    ctx = ToolContext(
        tool_call_id="tc-1",
        session_id="sess-parent",
        workspace_id=seeded,
        initiated_by=PrincipalRef(
            type="api_token",
            id="tok-1",
            display="ci",
            role="user",
            source="internal",
            user_id="u-1",
        ),
    )
    result = await session_toolset.call(
        tool_name="create_workspace_session",
        arguments={
            "workspace_id": seeded,
            "binding": {"kind": "agent", "agent_id": "code-reviewer"},
        },
        ctx=ctx,
    )
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert body["initiated_by"]["type"] == "api_token"
    assert body["initiated_by"]["id"] == "tok-1"
    assert body["initiated_by"]["source"] == "internal"


@pytest.mark.asyncio
async def test_create_session_with_no_manager_identity_fails_closed_to_an_ordinary_user(
    session_toolset,
    seeded,
) -> None:
    """No ctx at all (the MCP endpoint) -> an ordinary-user rank, never system."""
    result = await session_toolset.call(
        tool_name="create_workspace_session",
        arguments={
            "workspace_id": seeded,
            "binding": {"kind": "agent", "agent_id": "code-reviewer"},
        },
    )
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert body["initiated_by"]["type"] != "system"
    assert body["initiated_by"]["role"] == "user"


@pytest.mark.asyncio
async def test_a_session_started_over_mcp_runs_as_the_token_account(
    session_toolset,
    seeded,
) -> None:
    """No ctx and a known MCP caller: the session runs as the account behind the token (so it can use what that account can
    read), never unattributed and never system."""
    from primer.mcp.server import current_actor
    from primer.model.principal import Principal

    token = current_actor.set(Principal(type="api_token", id="tok-9", display="t", role="user", source="local", user_id="u-9"))
    try:
        result = await session_toolset.call(
            tool_name="create_workspace_session",
            arguments={"workspace_id": seeded, "binding": {"kind": "agent", "agent_id": "code-reviewer"}},
        )
    finally:
        current_actor.reset(token)
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert body["initiated_by"]["id"] == "tok-9" and body["initiated_by"]["user_id"] == "u-9"


async def test_previous_turn_cannot_start_a_child(session_toolset, sp, seeded):
    from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

    store = sp.get_storage(WorkspaceSession)
    caller = await store.create(WorkspaceSession(
        id="zz-caller", workspace_id=seeded, status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
        binding=AgentSessionBinding(agent_id="code-reviewer"), turn_no=2,
    ))
    ctx = ToolContext(
        tool_call_id="call", session_id=caller.id, workspace_id=seeded, turn_no=1,
        initiated_by=PrincipalRef(type="user", id="u-1", user_id="u-1", role="user", source="local", display="u1"),
    )
    args = {"workspace_id": seeded, "binding": {"kind": "agent", "agent_id": "code-reviewer"},
            "parent_session_id": "unrelated-lineage", "auto_start": False}
    stale = await session_toolset.call(tool_name="create_workspace_session", arguments=args, ctx=ctx)
    assert stale.is_error and json.loads(stale.output)["type"] == "conflict"

    from dataclasses import replace

    result = await session_toolset.call(tool_name="create_workspace_session", arguments=args, ctx=replace(ctx, turn_no=2))
    assert not result.is_error, result.output
    child = await store.get(json.loads(result.output)["id"])
    assert child.id < caller.id and child.parent_session_id == "unrelated-lineage"
    assert child.status == SessionStatus.CREATED and child.turn_status == "idle"
