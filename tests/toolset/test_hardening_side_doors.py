"""The toolset side doors the access review found (ACL-P1-HARD-A, security review findings 1, 2 and 4).

A toolset, a workspace template and a channel are owned rows (access spec 6.1): read access is the floor for listing,
calling or describing a toolset's tools and reading its sign-in status, for materialising a workspace from a template,
and for binding a workspace's replies to a channel. These tools asked the row nothing, so a user holding no level on a
private row could still read or act on it, and the template update and delete tools answered the guards' refusals before
the access check, naming what an unreadable row holds (a reserved id, a managing harness). A row the caller holds no
level on now answers exactly like a missing id (spec 6.2, 9).

Each exploit runs three ways: user B (a full user holding no level) is refused like a missing id, B may act once the
owner shares read (or full, for a write), and an admin always may.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.bootstrap.defaults import RESERVED_LOCAL_WORKSPACE_TEMPLATE
from primer.model.access import Access
from primer.model.channel import Channel
from primer.model.chat import Tool, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.provider import Toolset
from primer.model.workspace import (
    Workspace,
    WorkspaceProvider,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.model.yield_ import ToolContext
from primer.toolset._system_crud import (
    _call_tool_tool,
    _describe_tool_tool,
    _list_toolset_tools_tool,
    _toolset_auth_status_tool,
)
from primer.toolset.oauth.service import ToolsetAuthStatus
from primer.toolset.system import build_system_toolset
from primer.toolset.workspaces import build_workspaces_toolset
from tests.api.test_workspaces import _provider, _template, pr, sp, wsr  # noqa: F401  (fixtures)

TS = "ts-a"
OWNER = "user-a"
B = "user-b"


def _ctx(user_id: str, *, role: str = "user") -> ToolContext:
    """A run started by ``user_id`` with ``role``, from a session in workspace ``w``."""
    who = PrincipalRef(
        type="user", id=user_id, display=user_id, role=role, source="local", user_id=user_id,
    )
    return ToolContext(tool_call_id="tc", session_id="s", workspace_id="w", initiated_by=who)


class _FakeToolsetProvider:
    """The inner provider the registry serves for the private toolset row; records every dispatch."""

    lists_remotely = False

    def __init__(self, toolset_id: str) -> None:
        self._toolset_id = toolset_id
        self.dispatched: list[tuple[str, dict[str, Any]]] = []

    def required_role(self, tool_name: str) -> str:
        # As the MCP provider floors its operator-exposed tools: any authenticated user.
        return "user"

    def is_yielding(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal: str | None = None):
        yield Tool(
            id="echo", toolset_id=self._toolset_id, description="echo",
            args_schema={"type": "object"},
        )

    async def call(
        self, *, tool_name: str, arguments: dict[str, Any],
        principal: str | None = None, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        self.dispatched.append((tool_name, arguments))
        return ToolCallResult(output=json.dumps({"ok": True, "tool": tool_name}), is_error=False)


class _FakeOAuth:
    async def status(self, toolset: Toolset) -> ToolsetAuthStatus:
        return ToolsetAuthStatus(toolset_id=toolset.id, mode="none", state="not_applicable")


async def _seed_private_toolset(sp) -> None:
    """An MCP toolset owned by user-a: user-b holds no level on it."""
    await sp.get_storage(Toolset).create(
        Toolset(
            id=TS,
            provider="mcp",
            config={"transport": "http", "config": {"url": "http://mcp.example", "headers": {}, "oauth": {}}},
            access=Access(owners=[OWNER]),
        )
    )


@pytest.fixture
async def inner(sp, pr):  # noqa: F811
    """The private toolset row, and the registry's fake inner provider for it (by id)."""
    pr._toolset_factory = lambda row: _FakeToolsetProvider(row.id)  # type: ignore[attr-defined]
    await _seed_private_toolset(sp)
    providers: dict[str, _FakeToolsetProvider] = {}
    original = pr._toolset_factory  # type: ignore[attr-defined]

    def _factory(row):
        provider = original(row)
        providers[row.id] = provider
        return provider

    pr._toolset_factory = _factory  # type: ignore[attr-defined]
    return providers


def _body(result: ToolCallResult) -> dict[str, Any]:
    return json.loads(result.output)


def _not_found_toolset(message_id: str = TS) -> dict[str, Any]:
    return {"type": "not-found", "message": f"Toolset {message_id!r} does not exist"}


# ===========================================================================
# Finding 1: list_toolset_tools / call_tool / describe_tool / get_toolset_auth_status
# ===========================================================================


async def test_b_cannot_list_a_toolset_he_cannot_read(sp, pr, inner):  # noqa: F811
    handler = _list_toolset_tools_tool(pr)[1][1]
    refused = await handler({"toolset_id": TS}, ctx=_ctx(B))
    assert _body(refused) == _not_found_toolset()
    # The refusal comes before any provider contact: nothing listed, nothing dispatched.
    assert inner.get(TS) is None
    # A missing id answers the same way (the not-found text names the id the model spelled).
    missing = await handler({"toolset_id": "no-such-toolset"}, ctx=_ctx(B))
    assert _body(missing) == _not_found_toolset("no-such-toolset")


async def test_b_may_list_once_the_owner_shares_read(sp, pr, inner):  # noqa: F811
    row = await sp.get_storage(Toolset).get(TS)
    row.access = Access(owners=[OWNER], grants={B: "read"})
    await sp.get_storage(Toolset).update(row)
    handler = _list_toolset_tools_tool(pr)[1][1]
    ok = await handler({"toolset_id": TS}, ctx=_ctx(B))
    assert not ok.is_error
    assert _body(ok)["tools"][0]["id"] == "echo"


async def test_admin_lists_a_toolset(sp, pr, inner):  # noqa: F811
    handler = _list_toolset_tools_tool(pr)[1][1]
    ok = await handler({"toolset_id": TS}, ctx=_ctx("user-x", role="admin"))
    assert not ok.is_error
    assert inner[TS].dispatched == []  # listing does not dispatch


async def test_b_cannot_call_a_tool_of_a_toolset_he_cannot_read(sp, pr, inner):  # noqa: F811
    handler = _call_tool_tool(pr)[1][1]
    refused = await handler(
        {"toolset_id": TS, "tool_name": "echo", "arguments": {"x": 1}}, ctx=_ctx(B),
    )
    assert _body(refused) == _not_found_toolset()
    # Refused BEFORE dispatch: the inner provider is never reached.
    assert inner.get(TS) is None or inner[TS].dispatched == []


async def test_b_may_call_once_the_owner_shares_read(sp, pr, inner):  # noqa: F811
    row = await sp.get_storage(Toolset).get(TS)
    row.access = Access(owners=[OWNER], grants={B: "read"})
    await sp.get_storage(Toolset).update(row)
    handler = _call_tool_tool(pr)[1][1]
    ok = await handler(
        {"toolset_id": TS, "tool_name": "echo", "arguments": {"x": 1}}, ctx=_ctx(B),
    )
    assert not ok.is_error
    assert inner[TS].dispatched == [("echo", {"x": 1})]


async def test_admin_calls_a_tool_of_a_toolset(sp, pr, inner):  # noqa: F811
    handler = _call_tool_tool(pr)[1][1]
    ok = await handler(
        {"toolset_id": TS, "tool_name": "echo", "arguments": {}}, ctx=_ctx("user-x", role="admin"),
    )
    assert not ok.is_error
    assert inner[TS].dispatched == [("echo", {})]


async def test_b_cannot_describe_a_tool_of_a_toolset_he_cannot_read(sp, pr, inner):  # noqa: F811
    handler = _describe_tool_tool(pr)[1][1]
    refused = await handler({"toolset_id": TS, "tool_name": "echo"}, ctx=_ctx(B))
    # The describe door's missing answer is its own idiom ("no toolset <id>"): unreadable is not-new.
    assert _body(refused) == {"type": "not-found", "message": "no toolset 'ts-a'"}
    missing = await handler({"toolset_id": "nope", "tool_name": "echo"}, ctx=_ctx(B))
    assert _body(missing) == {"type": "not-found", "message": "no toolset 'nope'"}


async def test_b_may_describe_once_the_owner_shares_read(sp, pr, inner):  # noqa: F811
    row = await sp.get_storage(Toolset).get(TS)
    row.access = Access(owners=[OWNER], grants={B: "read"})
    await sp.get_storage(Toolset).update(row)
    handler = _describe_tool_tool(pr)[1][1]
    ok = await handler({"toolset_id": TS, "tool_name": "echo"}, ctx=_ctx(B))
    assert not ok.is_error
    assert _body(ok)["tool_name"] == "echo"


async def test_b_cannot_read_the_auth_status_of_a_toolset_he_cannot_read(sp, pr, inner):  # noqa: F811
    handler = _toolset_auth_status_tool(storage_provider=sp, oauth=_FakeOAuth(), built_in_ids=frozenset())[1][1]
    refused = await handler({"toolset_id": TS}, ctx=_ctx(B))
    assert _body(refused) == _not_found_toolset()
    missing = await handler({"toolset_id": "no-such-toolset"}, ctx=_ctx(B))
    assert _body(missing) == _not_found_toolset("no-such-toolset")


async def test_b_may_read_the_auth_status_once_the_owner_shares_read(sp, pr, inner):  # noqa: F811
    row = await sp.get_storage(Toolset).get(TS)
    row.access = Access(owners=[OWNER], grants={B: "read"})
    await sp.get_storage(Toolset).update(row)
    handler = _toolset_auth_status_tool(storage_provider=sp, oauth=_FakeOAuth(), built_in_ids=frozenset())[1][1]
    ok = await handler({"toolset_id": TS}, ctx=_ctx(B))
    assert not ok.is_error
    assert _body(ok)["state"] == "not_applicable"


async def test_admin_reads_the_auth_status_of_a_toolset(sp, pr, inner):  # noqa: F811
    handler = _toolset_auth_status_tool(storage_provider=sp, oauth=_FakeOAuth(), built_in_ids=frozenset())[1][1]
    ok = await handler({"toolset_id": TS}, ctx=_ctx("user-x", role="admin"))
    assert not ok.is_error


# ===========================================================================
# Finding 2: the create_workspace tool must hold read on the template
# ===========================================================================


async def test_b_cannot_materialise_a_workspace_from_a_template_he_cannot_read(sp, pr, wsr):  # noqa: F811
    await sp.get_storage(WorkspaceProvider).create(_provider())
    template = _template()
    template.access = Access(owners=[OWNER])
    await sp.get_storage(WorkspaceTemplate).create(template)
    # The calling session sits in the default workspace, so the reach refusal (a session outside
    # prime creates no workspace) stays out of the picture and the template check is the one asked.
    await sp.set_default_workspace_id("w")  # type: ignore[attr-defined]
    provider = build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)
    refused = await provider.call(
        tool_name="create_workspace", arguments={"id": "ws-b", "template_id": "tpl-1"},
        ctx=_ctx(B),
    )
    assert _body(refused) == {
        "type": "not-found",
        "message": "WorkspaceTemplate 'tpl-1' does not exist",
    }
    assert await sp.get_storage(Workspace).get("ws-b") is None


async def test_b_materialises_once_the_owner_shares_read(sp, pr, wsr):  # noqa: F811
    await sp.get_storage(WorkspaceProvider).create(_provider())
    template = _template()
    template.access = Access(owners=[OWNER], grants={B: "read"})
    await sp.get_storage(WorkspaceTemplate).create(template)
    await sp.set_default_workspace_id("w")  # type: ignore[attr-defined]
    provider = build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)
    ok = await provider.call(
        tool_name="create_workspace", arguments={"id": "ws-b", "template_id": "tpl-1"},
        ctx=_ctx(B),
    )
    assert not ok.is_error
    assert (await sp.get_storage(Workspace).get("ws-b")) is not None


async def test_admin_materialises_a_workspace_from_a_template(sp, pr, wsr):  # noqa: F811
    await sp.get_storage(WorkspaceProvider).create(_provider())
    template = _template()
    template.access = Access(owners=[OWNER])
    await sp.get_storage(WorkspaceTemplate).create(template)
    await sp.set_default_workspace_id("w")  # type: ignore[attr-defined]
    provider = build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)
    ok = await provider.call(
        tool_name="create_workspace", arguments={"id": "ws-admin", "template_id": "tpl-1"},
        ctx=_ctx("user-x", role="admin"),
    )
    assert not ok.is_error
    assert (await sp.get_storage(Workspace).get("ws-admin")) is not None


# ===========================================================================
# Finding 3: system__set_reply_binding must hold read on the named channel
# ===========================================================================


def _make_workspace(ws_id: str) -> Workspace:
    return Workspace(
        id=ws_id,
        template_id="tpl-1",
        provider_id="wp-1",
        created_at=datetime.now(timezone.utc),
        phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://localhost:5959", token=SecretStr("tok")),
    )


async def _seed_reply_world(sp) -> None:
    # B owns the workspace (a full write on it, which binding needs): the channel is the only level these tests vary.
    await sp.get_storage(Workspace).create(_make_workspace("w").model_copy(update={"access": Access(owners=[B])}))
    await sp.get_storage(Channel).create(
        Channel(id="ch-1", provider_id="cp-1", provider="slack", external_id="C1", access=Access(owners=[OWNER])),
    )


async def test_b_cannot_bind_a_workspace_to_a_channel_he_cannot_read(sp, pr):  # noqa: F811
    await _seed_reply_world(sp)
    provider = build_system_toolset(storage_provider=sp, provider_registry=pr)
    refused = await provider.call(
        tool_name="set_reply_binding",
        arguments={"workspace_id": "w", "channel_id": "ch-1"},
        ctx=_ctx(B),
    )
    assert _body(refused) == {"type": "not-found", "message": "Channel 'ch-1' does not exist"}
    # The answer is the missing-channel answer, and nothing was written.
    missing = await provider.call(
        tool_name="set_reply_binding",
        arguments={"workspace_id": "w", "channel_id": "no-such-channel"},
        ctx=_ctx(B),
    )
    assert _body(missing) == {"type": "not-found", "message": "Channel 'no-such-channel' does not exist"}
    assert (await sp.get_storage(Workspace).get("w")).reply_binding is None


async def test_b_binds_once_the_owner_shares_read(sp, pr):  # noqa: F811
    await _seed_reply_world(sp)
    channel = await sp.get_storage(Channel).get("ch-1")
    channel.access = Access(owners=[OWNER], grants={B: "read"})
    await sp.get_storage(Channel).update(channel)
    provider = build_system_toolset(storage_provider=sp, provider_registry=pr)
    ok = await provider.call(
        tool_name="set_reply_binding",
        arguments={"workspace_id": "w", "channel_id": "ch-1"},
        ctx=_ctx(B),
    )
    assert not ok.is_error
    row = await sp.get_storage(Workspace).get("w")
    assert row.reply_binding is not None and row.reply_binding.channel_id == "ch-1"


async def test_admin_binds_a_workspace_to_a_channel(sp, pr):  # noqa: F811
    await _seed_reply_world(sp)
    provider = build_system_toolset(storage_provider=sp, provider_registry=pr)
    ok = await provider.call(
        tool_name="set_reply_binding",
        arguments={"workspace_id": "w", "channel_id": "ch-1"},
        ctx=_ctx("user-x", role="admin"),
    )
    assert not ok.is_error


# ===========================================================================
# Finding 4: the template update and delete tools ask the access check before the guards
# ===========================================================================


async def test_b_cannot_learn_the_reserved_state_of_a_template_he_cannot_read(sp, pr, wsr):  # noqa: F811
    """The update guard's reserved-id refusal used to run before the access check, so a user
    holding no level on a template learned that a reserved id exists and is reserved. The access
    check now answers first: unreadable is a missing template, whatever the guards would say."""
    await sp.get_storage(WorkspaceProvider).create(_provider())
    template = WorkspaceTemplate(
        id=RESERVED_LOCAL_WORKSPACE_TEMPLATE, provider_id="local-1", description="default",
        access=Access(owners=[OWNER]),
    )
    await sp.get_storage(WorkspaceTemplate).create(template)
    provider = build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)
    refused = await provider.call(
        tool_name="update_workspace_template",
        arguments={
            "id": RESERVED_LOCAL_WORKSPACE_TEMPLATE,
            "entity": {"id": RESERVED_LOCAL_WORKSPACE_TEMPLATE, "provider_id": "local-1", "description": "default"},
        },
        ctx=_ctx(B),
    )
    assert _body(refused) == {
        "type": "not-found",
        "message": f"WorkspaceTemplate {RESERVED_LOCAL_WORKSPACE_TEMPLATE!r} does not exist",
    }
    assert (await sp.get_storage(WorkspaceTemplate).get(RESERVED_LOCAL_WORKSPACE_TEMPLATE)).access.owners == [OWNER]


async def test_b_cannot_learn_the_referenced_state_of_a_template_he_cannot_read(sp, pr, wsr):  # noqa: F811
    """The delete guards (managed-by refusal, reference blockers) run after the access check: an
    unreadable template answers as missing, and a readable one keeps the guards' refusals."""
    await sp.get_storage(WorkspaceProvider).create(_provider())
    template = WorkspaceTemplate(id="tpl-x", provider_id="local-1", description="tpl", access=Access(owners=[OWNER]))
    await sp.get_storage(WorkspaceTemplate).create(template)
    provider = build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)
    refused = await provider.call(
        tool_name="delete_workspace_template", arguments={"id": "tpl-x"}, ctx=_ctx(B),
    )
    assert _body(refused) == {"type": "not-found", "message": "WorkspaceTemplate 'tpl-x' does not exist"}
    assert (await sp.get_storage(WorkspaceTemplate).get("tpl-x")) is not None


async def test_b_updates_a_template_once_the_owner_shares_full(sp, pr, wsr):  # noqa: F811
    await sp.get_storage(WorkspaceProvider).create(_provider())
    template = WorkspaceTemplate(
        id="tpl-x", provider_id="local-1", description="tpl",
        access=Access(owners=[OWNER], grants={B: "full"}),
    )
    await sp.get_storage(WorkspaceTemplate).create(template)
    provider = build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)
    ok = await provider.call(
        tool_name="update_workspace_template",
        arguments={"id": "tpl-x", "entity": {"id": "tpl-x", "provider_id": "local-1", "description": "tuned"}},
        ctx=_ctx(B),
    )
    assert not ok.is_error
    assert (await sp.get_storage(WorkspaceTemplate).get("tpl-x")).description == "tuned"


async def test_admin_updates_and_deletes_templates(sp, pr, wsr):  # noqa: F811
    await sp.get_storage(WorkspaceProvider).create(_provider())
    template = WorkspaceTemplate(id="tpl-x", provider_id="local-1", description="tpl", access=Access(owners=[OWNER]))
    await sp.get_storage(WorkspaceTemplate).create(template)
    provider = build_workspaces_toolset(storage_provider=sp, workspace_registry=wsr)
    ok = await provider.call(
        tool_name="update_workspace_template",
        arguments={"id": "tpl-x", "entity": {"id": "tpl-x", "provider_id": "local-1", "description": "tuned"}},
        ctx=_ctx("user-x", role="admin"),
    )
    assert not ok.is_error
    deleted = await provider.call(
        tool_name="delete_workspace_template", arguments={"id": "tpl-x"},
        ctx=_ctx("user-x", role="admin"),
    )
    assert not deleted.is_error
    assert await sp.get_storage(WorkspaceTemplate).get("tpl-x") is None
