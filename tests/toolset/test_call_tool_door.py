"""``system__call_tool`` is the door to every platform tool for a user-role run, and it is not an escalation path (pivot P1, task 2).

``call_tool`` is role ``user``: the tool manager's RBAC floor only ever sees this OUTER tool, so the handler checks the INNER tool's
``required_role`` against the run's principal itself (``primer.agent.inner_dispatch``), BEFORE the approval gate, so a refused call never
draws an approval card. It passes the caller's own ``ToolContext`` to the inner tool and takes no ``principal`` argument: the only principal
an inner provider (an MCP toolset's OAuth token cache) is handed is the one the dispatch layer put on the context. ``list_toolset_tools``
takes no ``principal`` either, and ``ask_user`` and ``wait_for_event`` are role ``user`` so a user-role run can call them at all.

The escalation paths each have a test here: a user-role run reaching an admin tool through ``call_tool`` (directly, through the tool
manager, nested in another ``call_tool``, and through the approval gate), borrowing another identity through ``call_tool``'s or
``list_toolset_tools``' ``principal``, and a call made with no context, which ranks as an ordinary user.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.model.chat import Tool, ToolCallPart
from primer.model.common import dump_for_storage
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.system import build_system_toolset
from tests.agent.test_inner_dispatch import ADMIN, USER, _Recorder, _Session

# Re-export so pytest resolves the in-memory storage and the real provider registry the neighbouring call_tool tests use.
from tests.toolset.test_system import _llm, _required_policy, pr, sp  # noqa: F401


class _ListRecorder(_Recorder):
    """A ``_Recorder`` that also records the principal each listing was asked for."""

    def __init__(self, roles: dict[str, str]) -> None:
        super().__init__(roles)
        self.listed_as: list[str | None] = []

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        self.listed_as.append(principal)
        async for tool in super().list_tools(principal=principal):
            yield tool


class _Registry:
    """The registry ``call_tool`` resolves toolsets through: the real ``ProviderRegistry``, plus test toolsets under their own ids."""

    def __init__(self, real: ProviderRegistry, extra: dict[str, Any]) -> None:
        self._real = real
        self._extra = extra

    async def get_toolset(self, toolset_id: str) -> Any:
        if toolset_id in self._extra:
            return self._extra[toolset_id]
        return await self._real.get_toolset(toolset_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
def recorder() -> _ListRecorder:
    return _ListRecorder({"user_x": "user", "admin_x": "admin"})


@pytest.fixture
def system_toolset(sp, pr: ProviderRegistry, recorder: _ListRecorder):  # noqa: F811 - the re-exported fixtures
    provider = build_system_toolset(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=_Registry(pr, {"rec": recorder}),  # type: ignore[arg-type]
    )
    pr._system_toolset_provider = provider  # type: ignore[attr-defined]
    return provider


def _ctx(principal_ref: PrincipalRef | None, *, principal: str | None = None) -> ToolContext:
    return ToolContext(
        tool_call_id="call-1", session_id="sess-1", workspace_id="ws-1", initiated_by=principal_ref, principal=principal,
    )


async def _tool(toolset: Any, name: str) -> Tool:
    async for tool in toolset.list_tools():
        if tool.id == name:
            return tool
    raise AssertionError(f"the system toolset has no {name!r}")


async def _seed_llm_provider(system_toolset: Any) -> None:
    """Store the ``anthropic-1`` row, so a call that is refused is refused by the role check, not by a missing row."""
    created = await system_toolset.call(tool_name="create_llm_provider", arguments={"entity": dump_for_storage(_llm())})
    assert not created.is_error, created.output


def _forbidden(tool: str, toolset: str, need: str = "admin", have: str = "user") -> dict[str, str]:
    return {
        "type": "forbidden",
        "message": f"tool {tool!r} in toolset {toolset!r} requires the {need!r} role; this run's principal has the {have!r} role",
    }


# ---- the declarations ---------------------------------------------------------------------------------------------------------------------------------


async def test_call_tool_is_role_user_and_still_pauses(system_toolset):
    tool = await _tool(system_toolset, "call_tool")
    assert tool.required_role == "user" and tool.yields is True
    assert set(tool.args_schema["properties"]) == {"toolset_id", "tool_name", "arguments"}


async def test_list_toolset_tools_takes_no_principal(system_toolset):
    assert set((await _tool(system_toolset, "list_toolset_tools")).args_schema["properties"]) == {"toolset_id"}


async def test_ask_user_and_wait_for_event_are_role_user(system_toolset):
    assert {(await _tool(system_toolset, n)).required_role for n in ("ask_user", "wait_for_event")} == {"user"}


# ---- escalation: a user-role run reaching an admin tool through call_tool ----------------------------------------------------------------------------------


async def test_a_user_run_cannot_reach_an_admin_tool_through_call_tool(system_toolset):
    await _seed_llm_provider(system_toolset)
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={
        "toolset_id": "system", "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})
    assert r.is_error and json.loads(r.output) == _forbidden("get_llm_provider", "system")


async def test_a_user_run_cannot_run_an_admin_tool_of_any_toolset(system_toolset, recorder):
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={
        "toolset_id": "rec", "tool_name": "admin_x", "arguments": {}})
    assert r.is_error and json.loads(r.output) == _forbidden("admin_x", "rec")
    assert recorder.calls == [], "the inner provider must never be reached for a refused call"


async def test_an_admin_run_reaches_it_and_inner_errors_pass_through(system_toolset):
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(ADMIN), arguments={
        "toolset_id": "system", "tool_name": "get_llm_provider", "arguments": {"id": "missing"}})
    assert r.is_error and json.loads(r.output)["type"] == "not-found"
    await _seed_llm_provider(system_toolset)
    ok = await system_toolset.call(tool_name="call_tool", ctx=_ctx(ADMIN), arguments={
        "toolset_id": "system", "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})
    assert not ok.is_error and json.loads(ok.output)["id"] == "anthropic-1"


async def test_a_nested_call_tool_cannot_launder_the_role(system_toolset, recorder):
    """``call_tool`` is itself role user, so it runs as the inner tool of another ``call_tool``: it gets the same context and checks again."""
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={
        "toolset_id": "system", "tool_name": "call_tool",
        "arguments": {"toolset_id": "rec", "tool_name": "admin_x", "arguments": {}}})
    assert r.is_error and json.loads(r.output) == _forbidden("admin_x", "rec")
    assert recorder.calls == []


async def test_the_role_is_checked_before_the_approval_gate(system_toolset, sp):  # noqa: F811
    await sp.get_storage(ToolApprovalPolicy).create(_required_policy("system", "get_llm_provider"))
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={
        "toolset_id": "system", "tool_name": "get_llm_provider", "arguments": {"id": "x"}})
    # No YieldToWorker: a refused call draws no approval card an admin could approve on the user's behalf.
    assert json.loads(r.output)["type"] == "forbidden"


async def test_a_user_run_through_the_tool_manager_is_refused_an_admin_inner_tool(system_toolset, recorder):
    """The path an agent takes: the manager's floor passes ``call_tool`` (role user) and the handler refuses the admin inner tool."""
    mgr = ToolExecutionManager.for_workspace(
        toolset_providers={"system": system_toolset}, session=_Session("s-1", "ws-1"), tools=["system__call_tool"], initiated_by=USER,
    )
    for call_id, toolset_id, tool_name in (("c1", "rec", "admin_x"), ("c2", "system", "list_llm_providers")):
        part = await mgr.execute(ToolCallPart(id=call_id, name="system__call_tool", arguments={
            "toolset_id": toolset_id, "tool_name": tool_name, "arguments": {}}))
        assert part.error is True and json.loads(part.output) == _forbidden(tool_name, toolset_id)
    assert recorder.calls == []


async def test_a_user_run_passes_the_outer_floor_through_the_tool_manager(system_toolset, recorder):
    mgr = ToolExecutionManager.for_workspace(
        toolset_providers={"system": system_toolset}, session=_Session("s-1", "ws-1"), tools=["system__call_tool"], initiated_by=USER,
    )
    part = await mgr.execute(ToolCallPart(id="c1", name="system__call_tool", arguments={
        "toolset_id": "rec", "tool_name": "user_x", "arguments": {}}))
    assert part.error is False
    ((name, _args, _principal, ctx),) = recorder.calls
    assert name == "user_x" and (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.initiated_by) == ("c1", "s-1", "ws-1", USER)


# ---- escalation: a call made with no context ----------------------------------------------------------------------------------------------------------


async def test_a_call_with_no_context_ranks_as_an_ordinary_user(system_toolset, recorder):
    await _seed_llm_provider(system_toolset)
    refused = await system_toolset.call(tool_name="call_tool", arguments={
        "toolset_id": "system", "tool_name": "get_llm_provider", "arguments": {"id": "anthropic-1"}})
    assert refused.is_error and json.loads(refused.output) == _forbidden("get_llm_provider", "system")
    ran = await system_toolset.call(tool_name="call_tool", arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {}})
    assert not ran.is_error and recorder.calls == [("user_x", {}, None, None)]


async def test_a_context_without_an_initiator_ranks_as_an_ordinary_user(system_toolset, recorder):
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(None), arguments={
        "toolset_id": "rec", "tool_name": "admin_x", "arguments": {}})
    assert json.loads(r.output) == _forbidden("admin_x", "rec") and recorder.calls == []


async def test_a_bare_manager_has_no_context_to_give_so_its_call_tool_ranks_as_a_user(system_toolset, recorder):
    """A manager with no session and no park scope (``invoke_agent`` over MCP builds one) dispatches with no context: even an admin
    invoker is ranked as an ordinary user for the inner tool, the fail-closed side of "no context, no identity"."""
    mgr = ToolExecutionManager(toolset_providers={"system": system_toolset}, tools=["system__call_tool"], initiated_by=ADMIN)
    assert mgr.context_for("c1") is None
    part = await mgr.execute(ToolCallPart(id="c1", name="system__call_tool", arguments={
        "toolset_id": "rec", "tool_name": "admin_x", "arguments": {}}))
    assert part.error is True and json.loads(part.output) == _forbidden("admin_x", "rec") and recorder.calls == []


# ---- escalation: a model-chosen principal ---------------------------------------------------------------------------------------------------------------


async def test_a_principal_argument_is_never_forwarded(system_toolset, recorder):
    await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={
        "toolset_id": "rec", "tool_name": "user_x", "arguments": {}, "principal": "mallory"})
    assert recorder.calls[-1][2] is None
    await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER, principal="alice"), arguments={
        "toolset_id": "rec", "tool_name": "user_x", "arguments": {}, "principal": "mallory"})
    assert recorder.calls[-1][2] == "alice"


async def test_list_toolset_tools_never_forwards_a_model_chosen_principal(system_toolset, recorder):
    """The listing of an OAuth MCP toolset is keyed on the principal: a model must not list it as somebody else."""
    listed = await system_toolset.call(tool_name="list_toolset_tools", arguments={"toolset_id": "rec", "principal": "admin"})
    assert not listed.is_error and {t["id"] for t in json.loads(listed.output)["tools"]} == {"user_x", "admin_x"}
    await system_toolset.call(
        tool_name="list_toolset_tools", ctx=_ctx(USER, principal="alice"), arguments={"toolset_id": "rec", "principal": "admin"},
    )
    assert recorder.listed_as == [None, "alice"], "only the run's own principal (from the dispatch layer) may reach the provider"


# ---- the context and the approval park ----------------------------------------------------------------------------------------------------------------


async def test_the_inner_tool_receives_the_callers_context(system_toolset, recorder):
    ctx = _ctx(USER)
    await system_toolset.call(tool_name="call_tool", ctx=ctx, arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {}})
    assert recorder.calls[-1][3] is ctx


async def test_the_approval_park_names_the_inner_toolset_and_tool(system_toolset, sp):  # noqa: F811
    await sp.get_storage(ToolApprovalPolicy).create(_required_policy("rec", "user_x"))
    with pytest.raises(YieldToWorker) as exc:
        await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {}})
    assert exc.value.yielded.resume_metadata["via_call_tool"] == {"toolset_id": "rec", "tool_name": "user_x"}


async def test_an_unknown_toolset_is_not_found_before_the_role_check_or_the_gate(system_toolset, sp):  # noqa: F811
    await sp.get_storage(ToolApprovalPolicy).create(_required_policy("nope", "x"))
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "nope", "tool_name": "x", "arguments": {}})
    assert r.is_error and json.loads(r.output)["type"] == "not-found"
