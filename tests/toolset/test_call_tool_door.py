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
from primer.model.except_ import ProviderError
from primer.model.mcp_oauth import McpSignInRequiredError
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.system import build_system_toolset
from tests.agent.test_inner_dispatch import ADMIN, USER, _Recorder, _Session

from tests.toolset.test_system import _llm, _required_policy


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
def sp(fake_storage_provider):
    """The conftest's in-memory storage, whose ``find`` honours the predicate (audit G6-12).

    The neighbouring ``tests/toolset/test_system.py`` fake answers every ``find`` with every row, so a gate lookup under the WRONG
    ``(toolset_id, tool_name)`` still found the policy there and a test of the key could not fail.
    """
    return fake_storage_provider


@pytest.fixture
def pr(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type,return-value]
    )


@pytest.fixture
def recorder() -> _ListRecorder:
    return _ListRecorder({"user_x": "user", "admin_x": "admin"})


@pytest.fixture
def system_toolset(sp, pr: ProviderRegistry, recorder: _ListRecorder):
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


async def test_a_user_run_cannot_park_a_regex_schema_through_call_tool(system_toolset):
    """``ask_user`` is role user, so ``call_tool`` runs it for a user-role run, and a regex keyword in its ``response_schema`` is still refused
    before it parks (tests/toolset/test_ask_user_response_schema_regex.py): no YieldToWorker, a ``forbidden`` result."""
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={
        "toolset_id": "system", "tool_name": "ask_user",
        "arguments": {"prompt": "Name?", "response_schema": {"properties": {"name": {"type": "string", "pattern": "(a+)+$"}}}}})
    assert r.is_error and json.loads(r.output)["type"] == "forbidden"


@pytest.mark.parametrize("wait", [
    {"event_types": ["agent.*"]},
    {"event_types": ["session.ended"], "expr": "package primer.event_filter\nmatch := true\n"},
    {"event_types": ["session.ended"], "fields": [{"path": "payload.x", "op": "regex", "value": "a"}]},
], ids=["hidden-kind", "expr", "regex-matcher"])
async def test_a_user_run_cannot_widen_wait_for_event_through_call_tool(system_toolset, wait):
    """``wait_for_event`` is role user, so ``call_tool`` runs it for a user-role run, and the tool's own limits still hold for a non-admin: only
    the kinds the event feed shows it, no ``expr``, no ``regex`` matcher (tests/events/test_wait_for_event_kinds.py). Refused before anything is
    stored (this toolset's storage has no event store, so a wait that got further would raise)."""
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={
        "toolset_id": "system", "tool_name": "wait_for_event", "arguments": wait})
    assert r.is_error and json.loads(r.output)["type"] == "forbidden"


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


async def test_the_role_is_checked_before_the_approval_gate(system_toolset, sp):
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


async def test_the_approval_park_names_the_inner_toolset_and_tool(system_toolset, sp):
    await sp.get_storage(ToolApprovalPolicy).create(_required_policy("rec", "user_x"))
    with pytest.raises(YieldToWorker) as exc:
        await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {}})
    assert exc.value.yielded.resume_metadata["via_call_tool"] == {"toolset_id": "rec", "tool_name": "user_x"}


async def test_an_unknown_toolset_is_not_found_before_the_role_check_or_the_gate(system_toolset, sp):
    await sp.get_storage(ToolApprovalPolicy).create(_required_policy("nope", "x"))
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "nope", "tool_name": "x", "arguments": {}})
    assert r.is_error and json.loads(r.output)["type"] == "not-found"


async def test_the_gate_reads_the_inner_tools_own_key_and_no_other(system_toolset, sp, recorder):
    """The gate is looked up under the INNER ``(toolset_id, tool_name)``. On a storage whose ``find`` honours the predicate (audit G6-12), a policy
    on another tool of the same toolset, or on ``call_tool`` itself (the tool manager's gate on the OUTER call, not this handler's), leaves the
    inner call ungated: the suite's old fake returned every row for every lookup, so a lookup under the wrong key passed there."""
    store = sp.get_storage(ToolApprovalPolicy)
    await store.create(_required_policy("rec", "admin_x"))
    await store.create(_required_policy("system", "call_tool").model_copy(update={"id": "tap-outer"}))
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {}})
    assert not r.is_error, r.output
    assert [name for name, *_ in recorder.calls] == ["user_x"]


# ---- the gate's context: the run's agent (audit G6-7) ---------------------------------------------------------------------------------------------------

_BUILDER_ONLY = (
    "package primer.tool_approval\n"
    "default required := false\n"
    'required if input.agent_id == "agent-builder"\n'
)


def _manager_for(agent_id: str, system_toolset, recorder, sp) -> ToolExecutionManager:  # noqa: ANN001 - fixtures
    from primer.agent.approval import ApprovalResolver

    return ToolExecutionManager.for_workspace(
        toolset_providers={"system": system_toolset, "rec": recorder},
        session=_Session("s-1", "ws-1", agent_id=agent_id),
        approval_resolver=ApprovalResolver(storage=sp.get_storage(ToolApprovalPolicy)),
        tools=["system__call_tool", "rec__user_x"],
        initiated_by=USER,
    )


@pytest.mark.parametrize("name, arguments", [
    ("rec__user_x", {}),
    ("system__call_tool", {"toolset_id": "rec", "tool_name": "user_x", "arguments": {}}),
], ids=["direct", "through-call-tool"])
async def test_a_policy_on_the_runs_agent_gates_the_call_whatever_the_door(system_toolset, recorder, sp, name, arguments):
    """A Rego (or LLM judge) policy that reads ``input.agent_id`` sees the run's agent through ``call_tool`` as it does on a direct call: the manager
    puts the bound session's agent on the context (``ToolContext.agent_id``) and the door hands it to the gate. It used to hand ``None``, so a policy
    that gates one agent's calls gated its direct call and let the same call through the door, every seeded agent's only route."""
    from primer.model.tool_approval import PolicyApprovalConfig

    await sp.get_storage(ToolApprovalPolicy).create(ToolApprovalPolicy(
        id="p-builder", toolset_id="rec", tool_name="user_x", approval=PolicyApprovalConfig(policy=_BUILDER_ONLY),
    ))
    with pytest.raises(YieldToWorker) as parked:
        await _manager_for("agent-builder", system_toolset, recorder, sp).execute(ToolCallPart(id="c1", name=name, arguments=arguments))
    assert parked.value.yielded.resume_metadata["policy_id"] == "p-builder"
    assert recorder.calls == []

    part = await _manager_for("agent-planner", system_toolset, recorder, sp).execute(ToolCallPart(id="c2", name=name, arguments=arguments))
    assert part.error is False, part.output
    assert [call_name for call_name, *_ in recorder.calls] == ["user_x"], "another agent's call is not gated"


def test_the_context_carries_the_bound_sessions_agent():
    mgr = ToolExecutionManager.for_workspace(toolset_providers={}, session=_Session("s-1", "ws-1", agent_id="agent-builder"), initiated_by=USER)
    ctx = mgr.context_for("c1")
    assert ctx is not None and ctx.agent_id == "agent-builder"
    bare = ToolExecutionManager(toolset_providers={}, initiated_by=USER)
    assert bare.context_for("c1") is None


# ---- the inner tool is looked up in its toolset's listing first (audit G6-8) ---------------------------------------------------------------------------


@pytest.mark.parametrize("principal_ref", [USER, ADMIN], ids=["user", "admin"])
async def test_an_unknown_tool_is_not_found_whoever_runs_it(system_toolset, principal_ref):
    """A tool the toolset does not list is ``not-found`` for every run. It used to reach the role check first, where an unknown name fails closed
    to ``admin``: a user-role run that mistyped a name was told the tool "requires the 'admin' role" (a refusal errors.md says not to retry), and
    an admin run got ``tool-call-error`` from the provider instead."""
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(principal_ref), arguments={
        "toolset_id": "system", "tool_name": "create_agnet", "arguments": {}})
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "not-found", body
    assert "'create_agnet'" in body["message"] and "'system'" in body["message"] and "create_agent" in body["message"], body


async def test_an_unknown_tool_draws_no_approval_card(system_toolset, sp, recorder):
    """A policy stored for a name the toolset does not list gates nothing that could run: the lookup answers before the gate, so no card is parked
    for a call that cannot run."""
    await sp.get_storage(ToolApprovalPolicy).create(_required_policy("rec", "ghost"))
    r = await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "rec", "tool_name": "ghost", "arguments": {}})
    assert r.is_error and json.loads(r.output)["type"] == "not-found"
    assert recorder.calls == []


async def test_the_listing_is_made_as_the_runs_own_principal(system_toolset, recorder):
    """The lookup lists the toolset as the run's principal string (the context's), the one the call itself is made with, never a model's."""
    await system_toolset.call(tool_name="call_tool", ctx=_ctx(USER, principal="alice"), arguments={
        "toolset_id": "rec", "tool_name": "user_x", "arguments": {}, "principal": "mallory"})
    assert recorder.listed_as == ["alice"]
    assert recorder.calls[-1][2] == "alice"


class _Unlistable(_ListRecorder):
    """A toolset whose listing fails (an MCP server that is down, or one that needs a sign-in); its call is never reached."""

    def __init__(self, error: Exception) -> None:
        super().__init__({"user_x": "user"})
        self.error = error

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        raise self.error
        yield  # pragma: no cover - an async generator


@pytest.mark.parametrize("error, expected", [
    (ProviderError("the MCP server is down"), "provider-error"),
    (McpSignInRequiredError("remote"), "sign-in-required"),
], ids=["down", "needs-sign-in"])
async def test_a_toolset_that_cannot_list_answers_before_the_role_check(sp, pr, error, expected):
    unlistable = _Unlistable(error)
    toolset = build_system_toolset(storage_provider=sp, provider_registry=_Registry(pr, {"remote": unlistable}))  # type: ignore[arg-type]
    r = await toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "remote", "tool_name": "user_x", "arguments": {}})
    assert r.is_error and json.loads(r.output) == {"type": expected, "message": str(error)}
    assert unlistable.calls == []


async def test_a_toolset_that_does_not_list_in_time_is_a_provider_error_and_its_listing_is_closed(sp, pr, monkeypatch):
    """The lookup waits as long as a turn waits for the same listing (``tool_listing.TURN_TOOLSET_LIST_TIMEOUT_S``, a stdio server's cold start takes
    tens of seconds), not the 10 s policy-check clock, and the listing's generator is closed when it is given up on."""
    import asyncio

    closed: list[bool] = []

    class _Slow(_ListRecorder):
        async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
            try:
                await asyncio.sleep(5)
                yield Tool(id="user_x", description="late", toolset_id="slow", args_schema={})
            finally:
                closed.append(True)

    monkeypatch.setattr("primer.agent.tool_listing.TURN_TOOLSET_LIST_TIMEOUT_S", 0.05)
    slow = _Slow({"user_x": "user"})
    toolset = build_system_toolset(storage_provider=sp, provider_registry=_Registry(pr, {"slow": slow}))  # type: ignore[arg-type]
    r = await toolset.call(tool_name="call_tool", ctx=_ctx(USER), arguments={"toolset_id": "slow", "tool_name": "user_x", "arguments": {}})
    body = json.loads(r.output)
    assert r.is_error and body == {"type": "provider-error", "message": "toolset 'slow' did not list its tools within 0.05 seconds"}, body
    assert slow.calls == [] and closed == [True]


def test_the_lookup_waits_as_long_as_a_turn_waits_for_the_same_listing():
    from primer.agent import tool_listing
    from primer.agent.tool_schemas import LIST_TIMEOUT_S

    assert tool_listing.TURN_TOOLSET_LIST_TIMEOUT_S > LIST_TIMEOUT_S


# ---- the lookup's listing is kept per toolset and principal (review of #851) ------------------------------------------------------------------------------


async def _call(toolset, principal: str | None, tool_name: str = "user_x"):
    return await toolset.call(tool_name="call_tool", ctx=_ctx(USER, principal=principal), arguments={
        "toolset_id": "rec", "tool_name": tool_name, "arguments": {}})


async def test_the_listing_is_kept_per_toolset_and_principal(system_toolset, recorder):
    """One ``tools/list`` per toolset and principal string for ``CALL_TOOL_LISTING_TTL_S``, not one per call: the lookup runs on every call."""
    for _ in range(3):
        assert not (await _call(system_toolset, "alice")).is_error
    assert recorder.listed_as == ["alice"]
    assert not (await _call(system_toolset, "bob")).is_error
    assert recorder.listed_as == ["alice", "bob"], "another principal's listing is its own"
    assert len(recorder.calls) == 4


async def test_a_kept_listing_expires(system_toolset, recorder, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("primer.toolset._system_crud.time.monotonic", lambda: clock[0])
    await _call(system_toolset, "alice")
    clock[0] += 29.0
    await _call(system_toolset, "alice")
    assert recorder.listed_as == ["alice"]
    clock[0] += 2.0
    await _call(system_toolset, "alice")
    assert recorder.listed_as == ["alice", "alice"], "listed again once the kept listing is 30 s old"


async def test_a_name_the_kept_listing_lacks_is_listed_again_before_it_is_not_found(system_toolset, recorder):
    """A tool the toolset gained since its listing was kept is found: a miss lists again at once instead of answering ``not-found`` from the old listing."""
    await _call(system_toolset, "alice")
    recorder.roles["new_x"] = "user"
    assert not (await _call(system_toolset, "alice", "new_x")).is_error
    assert recorder.listed_as == ["alice", "alice"]
    missing = await _call(system_toolset, "alice", "ghost")
    assert json.loads(missing.output)["type"] == "not-found"
