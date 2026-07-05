"""MCP ``create_workspace_session`` attribution — Layer 3 Task 2 (§6.6, §8.1).

The ``create_workspace_session`` tool is a ctx-taking handler:
``InternalToolsetProvider`` injects the enclosing ``ToolContext`` when the
handler declares it, and the handler stamps
``ctx.initiated_by or PrincipalRef.system()`` onto the created session so
a sub-session an agent spawns inherits the enclosing run's attribution,
while a caller with no threaded identity falls back to the system
principal rather than fabricating a ``user`` attribution.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.api.registries import WorkspaceRegistry
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage, OffsetPageResponse
from primer.model.workspace import (
    LocalWorkspaceConfig,
    WorkspaceProvider,
    WorkspaceProviderType,
)
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import build_workspaces_toolset


# ===========================================================================
# In-memory fakes (minimal subset copied from tests/toolset/test_workspaces.py)
# ===========================================================================


class _Storage:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, id):
        return self._data.get(id)

    async def create(self, e):
        if e.id in self._data:
            raise ConflictError(f"id {e.id!r} already exists")
        self._data[e.id] = e
        return e

    async def update(self, e):
        if e.id not in self._data:
            raise NotFoundError(f"no entity with id {e.id!r}")
        self._data[e.id] = e
        return e

    async def delete(self, id):
        if id not in self._data:
            raise NotFoundError(f"no entity with id {id!r}")
        del self._data[id]

    async def list(self, page, *, order_by=None):
        items = list(self._data.values())
        if isinstance(page, OffsetPage):
            return OffsetPageResponse(
                offset=page.offset,
                length=len(items[page.offset : page.offset + page.length]),
                total=len(items),
                items=items[page.offset : page.offset + page.length],
            )
        return OffsetPageResponse(
            offset=0, length=len(items), total=len(items), items=items
        )

    async def find(self, predicate, page, *, order_by=None):
        return await self.list(page, order_by=order_by)


class _SP:
    def __init__(self) -> None:
        self._stores: dict[type, _Storage] = {}

    def get_storage(self, cls):
        return self._stores.setdefault(cls, _Storage())


class _LiveWorkspace:
    def __init__(self, workspace_id="ws-stub") -> None:
        from pydantic import SecretStr

        from primer.model.workspace import WorkspaceRuntimeMeta

        self.id = workspace_id
        self._sessions: dict[str, Any] = {}
        self.runtime_meta = WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959/", token=SecretStr("t"),
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

    async def create(self, template, *, overrides=None, resolvers=None):
        ws = _LiveWorkspace("ws-stub")
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

    sp.get_storage(Agent)._data[agent_id] = Agent(
        id=agent_id,
        description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )


def _provider() -> WorkspaceProvider:
    return WorkspaceProvider(
        id="local-1",
        provider=WorkspaceProviderType.LOCAL,
        config=LocalWorkspaceConfig(root_path="/tmp/x"),
    )


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def sp() -> _SP:
    return _SP()


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
    await session_toolset.call(
        tool_name="create_workspace_provider",
        arguments={"entity": _provider().model_dump(mode="json")},
    )
    await session_toolset.call(
        tool_name="create_workspace_template",
        arguments={
            "entity": {"id": "tpl-1", "description": "dev", "provider_id": "local-1"},
        },
    )
    create = await session_toolset.call(
        tool_name="create_workspace",
        arguments={"id": "ws-stub", "template_id": "tpl-1"},
    )
    assert not create.is_error, create.output
    _seed_agent(sp)
    return "ws-stub"


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_create_session_stamps_ctx_initiated_by(
    session_toolset, seeded,
) -> None:
    """A ctx carrying a per-call identity stamps the child row from it."""
    ctx = ToolContext(
        tool_call_id="tc-1",
        session_id="sess-parent",
        workspace_id=seeded,
        initiated_by=PrincipalRef(
            type="api_token", id="tok-1", display="ci", role="user",
            source="internal",
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
async def test_create_session_falls_back_to_system_with_no_manager_identity(
    session_toolset, seeded,
) -> None:
    """No ctx at all (or a ctx with no initiated_by) -> system fallback."""
    result = await session_toolset.call(
        tool_name="create_workspace_session",
        arguments={
            "workspace_id": seeded,
            "binding": {"kind": "agent", "agent_id": "code-reviewer"},
        },
    )
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert body["initiated_by"]["type"] == "system"
