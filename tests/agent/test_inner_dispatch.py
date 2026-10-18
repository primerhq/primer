"""The inner-dispatch helper and the run context of a toolset call (pivot P1, task 1).

``system__call_tool`` runs a tool of any toolset for the run that called it. ``primer.agent.inner_dispatch`` is the one place that checks the
INNER tool's ``required_role`` against the run's principal (``ToolContext.initiated_by``; no context, or a context without one, ranks as an
ordinary user, never as the system) and then calls the inner provider with the caller's own ``ToolContext``, unchanged. That context is the
one ``ToolExecutionManager.context_for`` builds for the outer call, carrying the principal string the outer dispatch received
(``ToolContext.principal``), which is the only principal the inner provider is ever handed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from primer.agent.inner_dispatch import FORBIDDEN_ERROR_TYPE, dispatch_inner_tool, inner_role_refusal, run_principal
from primer.agent.tool_manager import ToolExecutionManager
from primer.int.toolset import ToolsetProvider
from primer.model.chat import Tool, ToolCallPart, ToolCallResult
from primer.model.except_ import NotFoundError
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker

ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")
RESTRICTED = PrincipalRef(type="user", id="u-r", display="restricted", role="restricted", source="local")

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": True}


class _Recorder(ToolsetProvider):
    """A toolset that lists one tool per name of ``roles`` and records ``(tool_name, arguments, principal, ctx)`` per call.

    ``required_role`` answers from ``roles``, else ``"admin"`` (the fail-closed default of the base class).
    """

    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles
        self.calls: list[tuple[str, dict[str, Any], str | None, ToolContext | None]] = []

    def required_role(self, tool_name: str) -> str:
        return self.roles.get(tool_name, "admin")

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for name in self.roles:
            yield Tool(id=name, description=f"the {name} tool", toolset_id="rec", args_schema=_SCHEMA)

    async def call(
        self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        self.calls.append((tool_name, arguments, principal, ctx))
        return ToolCallResult(output=json.dumps({"ran": tool_name}), is_error=False)


class _Yielding(ToolsetProvider):
    """A role-user toolset whose one tool pauses the turn, as ``ask_user`` does."""

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="wait", description="pauses", toolset_id="y", args_schema=_SCHEMA, yields=True)

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        raise YieldToWorker(Yielded(tool_name=tool_name, event_key=f"k:{ctx.tool_call_id}"), tool_call_id=ctx.tool_call_id)


class _Failing(ToolsetProvider):
    """A role-user toolset whose one tool raises a ``PrimerError``."""

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="boom", description="raises", toolset_id="f", args_schema=_SCHEMA)

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        raise NotFoundError("no such row")


@dataclass
class _Session:
    """The duck-typed workspace session ``ToolExecutionManager.for_workspace`` reads."""

    session_id: str
    workspace_id: str
    agent_id: str = "agent-1"
    workspace_tools: list = field(default_factory=list)


def _ctx(principal_ref: PrincipalRef | None, *, principal: str | None = None) -> ToolContext:
    return ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=principal_ref, principal=principal)


# ---- the role check -------------------------------------------------------------------------------------------------------------------------------------


async def test_a_user_principal_is_refused_an_admin_tool():
    p = _Recorder({"create_x": "admin"})
    ctx = ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=USER)
    r = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="create_x", arguments={}, ctx=ctx)
    assert r.is_error and json.loads(r.output) == {
        "type": "forbidden",
        "message": "tool 'create_x' in toolset 't' requires the 'admin' role; this run's principal has the 'user' role",
    }
    assert p.calls == [], "the inner tool must never run for a refused call"


async def test_an_admin_principal_runs_it_with_the_same_context():
    p = _Recorder({"create_x": "admin"})
    ctx = ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=ADMIN, turn_no=3)
    r = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="create_x", arguments={"a": 1}, ctx=ctx)
    assert not r.is_error and p.calls == [("create_x", {"a": 1}, None, ctx)]
    assert p.calls[0][3] is ctx, "the inner tool gets the caller's own context object, unchanged"


async def test_no_context_ranks_as_an_unattributed_user():
    p = _Recorder({"admin_x": "admin", "user_x": "user"})
    refused = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="admin_x", arguments={}, ctx=None)
    assert refused.is_error and json.loads(refused.output)["type"] == "forbidden"
    ran = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="user_x", arguments={}, ctx=None)
    assert not ran.is_error and p.calls == [("user_x", {}, None, None)]


async def test_a_context_without_a_principal_ranks_as_an_unattributed_user_never_as_the_system():
    """A context whose ``initiated_by`` is unset is an unknown caller: it is ranked as an ordinary user, not waved through like ``system``."""
    p = _Recorder({"admin_x": "admin", "user_x": "user"})
    refused = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="admin_x", arguments={}, ctx=_ctx(None))
    assert json.loads(refused.output) == {
        "type": "forbidden",
        "message": "tool 'admin_x' in toolset 't' requires the 'admin' role; this run's principal has the 'user' role",
    }
    ran = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="user_x", arguments={}, ctx=_ctx(None))
    assert not ran.is_error and [c[0] for c in p.calls] == ["user_x"]


async def test_an_undeclared_inner_tool_fails_closed_to_admin():
    """A provider that declares no role for a tool answers ``admin`` (the base class default): a user run is refused it."""
    p = _Recorder({})
    r = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="anything", arguments={}, ctx=_ctx(USER))
    assert json.loads(r.output)["type"] == FORBIDDEN_ERROR_TYPE == "forbidden" and p.calls == []


async def test_a_restricted_principal_is_refused_a_user_tool_and_named_by_its_role():
    p = _Recorder({"user_x": "user"})
    r = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="user_x", arguments={}, ctx=_ctx(RESTRICTED))
    assert json.loads(r.output)["message"] == (
        "tool 'user_x' in toolset 't' requires the 'user' role; this run's principal has the 'restricted' role"
    )
    assert p.calls == []


async def test_a_principal_without_a_role_is_named_by_its_type_and_refused():
    """No role ranks below every floor (``primer.authz._role_allows``); the message names the principal's type instead."""
    token = PrincipalRef(type="api_token", id="tok-1", display="token", role=None, source="internal")
    p = _Recorder({"user_x": "user"})
    r = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="user_x", arguments={}, ctx=_ctx(token))
    assert json.loads(r.output)["message"].endswith("this run's principal has the 'api_token' role") and p.calls == []


async def test_the_system_principal_runs_an_admin_tool():
    p = _Recorder({"admin_x": "admin"})
    r = await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="admin_x", arguments={}, ctx=_ctx(PrincipalRef.system()))
    assert not r.is_error and [c[0] for c in p.calls] == ["admin_x"]


def test_run_principal_and_inner_role_refusal():
    assert run_principal(None) == PrincipalRef.unattributed()
    assert run_principal(_ctx(None)) == PrincipalRef.unattributed()
    assert run_principal(_ctx(ADMIN)) is ADMIN
    p = _Recorder({"user_x": "user", "admin_x": "admin"})
    assert inner_role_refusal(p, toolset_id="t", tool_name="user_x", ctx=_ctx(USER)) is None
    assert inner_role_refusal(p, toolset_id="t", tool_name="admin_x", ctx=_ctx(ADMIN)) is None
    refusal = inner_role_refusal(p, toolset_id="t", tool_name="admin_x", ctx=_ctx(USER))
    assert refusal is not None and refusal.is_error and json.loads(refusal.output)["type"] == "forbidden"


# ---- the dispatch ---------------------------------------------------------------------------------------------------------------------------------------


async def test_the_context_principal_string_is_forwarded():
    p = _Recorder({"user_x": "user"})
    ctx = ToolContext(tool_call_id="c1", session_id="s1", workspace_id=None, initiated_by=USER, principal="alice")
    await dispatch_inner_tool(provider=p, toolset_id="t", tool_name="user_x", arguments={}, ctx=ctx)
    assert p.calls[0][2] == "alice"


async def test_a_pause_from_the_inner_tool_propagates():
    with pytest.raises(YieldToWorker) as exc:
        await dispatch_inner_tool(provider=_Yielding(), toolset_id="t", tool_name="wait", arguments={}, ctx=_ctx(USER))
    assert exc.value.tool_call_id == "c1" and exc.value.yielded.tool_name == "wait"


async def test_a_primer_error_from_the_inner_tool_propagates():
    with pytest.raises(NotFoundError):
        await dispatch_inner_tool(provider=_Failing(), toolset_id="t", tool_name="boom", arguments={}, ctx=_ctx(USER))


# ---- the context of a toolset call ----------------------------------------------------------------------------------------------------------------------


def test_context_for_matches_the_dispatch_context():
    mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=_Session("s-1", "ws-1"), initiated_by=USER, turn_no=4)
    ctx = mgr.context_for("call-1", principal="bob")
    assert (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.initiated_by, ctx.turn_no, ctx.principal) == (
        "call-1", "s-1", "ws-1", USER, 4, "bob",
    )
    assert ToolExecutionManager(chat_id="chat-1", initiated_by=USER).context_for("c").chat_id == "chat-1"
    assert ToolExecutionManager(initiated_by=USER).context_for("c") is None


def test_context_for_carries_the_inform_sink_the_graph_services_and_the_chat_branch():
    async def sink(message: str) -> int:
        return 1

    services = object()
    mgr = ToolExecutionManager.for_workspace(
        toolset_providers={}, session=_Session("s-1", "ws-1"), initiated_by=USER, graph_invocation_services=services,
    )
    mgr.set_inform_sink(sink)
    ctx = mgr.context_for("call-1")
    assert (ctx.inform, ctx.graph_services, ctx.chat_id, ctx.principal, ctx.parked_at) == (sink, services, None, None, None)

    chat = ToolExecutionManager(chat_id="chat-1", initiated_by=ADMIN, turn_no=2)
    chat.set_inform_sink(sink)
    ctx = chat.context_for("call-2", principal="carol")
    assert (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.chat_id, ctx.inform, ctx.graph_services) == (
        "call-2", None, None, "chat-1", sink, None,
    )
    assert (ctx.initiated_by, ctx.turn_no, ctx.principal) == (ADMIN, 2, "carol")


async def test_dispatch_hands_the_provider_the_principal_on_its_context():
    p = _Recorder({"user_x": "user"})
    mgr = ToolExecutionManager.for_workspace(toolset_providers={"rec": p}, session=_Session("s-1", "ws-1"), initiated_by=USER)
    await mgr.execute(ToolCallPart(id="c1", name="rec__user_x", arguments={}), principal="bob")
    assert p.calls[0][2] == "bob" and p.calls[0][3].principal == "bob"
    assert (p.calls[0][3].tool_call_id, p.calls[0][3].session_id, p.calls[0][3].initiated_by) == ("c1", "s-1", USER)
