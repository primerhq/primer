"""The approved re-run of a ``call_tool`` inner tool runs as the resumed run, and the inner role is checked again (pivot P1, task 4).

``system__call_tool`` gates the tool it was asked to run and parks with ``via_call_tool = {toolset_id, tool_name}``. When the gate is approved,
``primer.worker.yield_runtime._resume_call_tool_dispatch`` runs that inner tool. It goes through the helper the ``call_tool`` handler uses,
``primer.agent.inner_dispatch.dispatch_inner_tool``, so that:

* the provider gets the context of the RESUMED run (``tool_manager.context_for``: the session, identity and turn of the manager that is resuming
  it), never a value stored with the park, and a manager that has none gives no context at all;
* a ``principal`` stored in the park (a park from before the pivot carries one a model chose) is never read;
* the inner tool's ``required_role`` is checked against that context's principal again: the approver decided about THIS call, not about who may
  make it, so a stale or forged park cannot run an admin tool for a user-role run;
* a pause of the inner tool (``YieldToWorker``) is not swallowed into an error result. It propagates, and the resume coordinator re-parks the
  session on the tool's own event (``tests/worker/test_approval_yield_repark.py`` pins that half).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import ToolCallPart
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.toolset.system import build_system_toolset
from primer.worker.frames import AgentFrame, AgentResumeContext
from primer.worker.yield_runtime import _resume_call_tool_dispatch, _resume_tool_approval
from tests.agent.test_inner_dispatch import ADMIN, USER, _Failing, _Recorder, _Session, _Yielding
from tests.toolset.test_call_tool_door import _Registry as _DoorRegistry

# Re-export so pytest resolves the in-memory storage and the real provider registry the neighbouring call_tool tests use.
from tests.toolset.test_system import _required_policy, pr, sp  # noqa: F401

#: The park's call: ``call_tool`` parks the INNER tool's bare name and arguments under the id of the ``call_tool`` call itself.
CALL = ToolCallPart(id="call-outer", name="user_x", arguments={"a": 1})


class _Registry:
    """The provider registry a manager holds (``_provider_registry``): every toolset id resolves to the one provider under test."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def get_toolset(self, toolset_id: str) -> Any:
        return self.provider


class _Manager:
    """The manager of the RESUMED run, as far as the re-run reads it: the registry, and the context of a call (``context_for``)."""

    def __init__(self, provider: Any, initiated_by: PrincipalRef | None) -> None:
        self._provider_registry = _Registry(provider)
        self._initiated_by = initiated_by
        self.context_requests: list[tuple[str, str | None]] = []

    def context_for(self, call_id: str, *, principal: str | None = None) -> ToolContext:
        self.context_requests.append((call_id, principal))
        return ToolContext(
            tool_call_id=call_id, session_id="s-1", workspace_id="ws-1", initiated_by=self._initiated_by, principal=principal,
        )


class _NoContextManager:
    """A manager that cannot give a context (a bare one, or a duck-typed one that predates ``context_for``): the registry and nothing else."""

    def __init__(self, provider: Any) -> None:
        self._provider_registry = _Registry(provider)


def _via(tool_name: str = "user_x") -> dict[str, str]:
    return {"toolset_id": "rec", "tool_name": tool_name}


# ---- the context -------------------------------------------------------------------------------------------------------------------------------------


async def test_the_approved_rerun_passes_the_resumed_runs_context():
    rec = _Recorder({"user_x": "user"})
    manager = _Manager(rec, USER)
    part = await _resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=manager)
    assert part.error is False and part.id == "call-outer" and json.loads(part.output) == {"ran": "user_x"}
    ((name, arguments, principal, ctx),) = rec.calls
    assert (name, arguments, principal) == ("user_x", {"a": 1}, None)
    assert (ctx.session_id, ctx.workspace_id, ctx.initiated_by, ctx.tool_call_id) == ("s-1", "ws-1", USER, "call-outer")
    assert manager.context_requests == [("call-outer", None)], "the context is the resumed run's, asked for under the park's call id"


async def test_the_rerun_through_a_real_manager_carries_the_runs_session_identity_and_turn():
    """The production shape: the real ``ToolExecutionManager.context_for``, not a stand-in for it."""
    rec = _Recorder({"user_x": "user"})
    manager = ToolExecutionManager.for_workspace(
        toolset_providers={}, session=_Session("s-9", "ws-9"), initiated_by=USER, provider_registry=_Registry(rec), turn_no=4,
    )
    part = await _resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=manager)
    assert part.error is False
    ((_name, _arguments, principal, ctx),) = rec.calls
    assert principal is None
    assert (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.initiated_by, ctx.turn_no) == ("call-outer", "s-9", "ws-9", USER, 4)


async def test_a_manager_that_cannot_give_a_context_ranks_the_run_as_an_unattributed_user():
    """No context, no identity: the inner provider gets none, an ordinary-user tool runs, and an admin tool is refused (never ranked as the system)."""
    rec = _Recorder({"user_x": "user", "admin_x": "admin"})
    ran = await _resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=_NoContextManager(rec))
    assert ran.error is False and rec.calls == [("user_x", {"a": 1}, None, None)]
    refused = await _resume_call_tool_dispatch(
        via=_via("admin_x"), original_call=ToolCallPart(id="call-outer", name="admin_x", arguments={}), tool_manager=_NoContextManager(rec),
    )
    assert refused.error is True and json.loads(refused.output)["type"] == "forbidden" and len(rec.calls) == 1


async def test_the_rerun_hands_the_resumed_runs_principal_string_to_the_context():
    """A graph executor dispatches with a principal string (an MCP toolset keys its per-user state on it); the re-run asks the manager for the context with THAT, and the
    provider is handed it. The agent session has none and asks as before (the cases above)."""
    rec = _Recorder({"user_x": "user"})
    manager = _Manager(rec, USER)
    part = await _resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=manager, principal="alice")
    assert part.error is False
    ((_name, _arguments, principal, ctx),) = rec.calls
    assert principal == "alice" and ctx.principal == "alice"
    assert manager.context_requests == [("call-outer", "alice")]


# ---- the principal ------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("real_manager", [False, True], ids=["stand-in-manager", "real-manager"])
async def test_a_legacy_park_principal_is_not_forwarded(real_manager: bool):
    """A park from before the pivot: ``via_call_tool`` carries the toolset and a principal the model chose (and no tool name). Neither the provider's
    principal nor the context it is handed may be that value: the stand-in keeps whatever principal it is asked for, and the real manager sets it on
    the context, so a re-run that asked for ``via['principal']`` is seen either way."""
    rec = _Recorder({"user_x": "user"})
    manager = (
        ToolExecutionManager.for_workspace(
            toolset_providers={}, session=_Session("s-1", "ws-1"), initiated_by=USER, provider_registry=_Registry(rec),
        )
        if real_manager else _Manager(rec, USER)
    )
    await _resume_call_tool_dispatch(via={"toolset_id": "rec", "principal": "mallory"}, original_call=CALL, tool_manager=manager)
    ((_name, _arguments, principal, ctx),) = rec.calls
    assert principal is None, "the principal an MCP toolset keys its OAuth tokens on is never one stored with the park"
    assert ctx.principal is None
    if not real_manager:
        assert manager.context_requests == [("call-outer", None)]


# ---- the role -----------------------------------------------------------------------------------------------------------------------------------------


async def test_the_approved_rerun_rechecks_the_inner_role():
    rec = _Recorder({"user_x": "admin"})
    part = await _resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=_Manager(rec, USER))
    assert part.error is True and part.id == "call-outer"
    assert json.loads(part.output) == {
        "type": "forbidden",
        "message": "tool 'user_x' in toolset 'rec' requires the 'admin' role; this run's principal has the 'user' role",
    }
    assert rec.calls == [], "the approval does not run a tool the resumed run's principal may not run"


async def test_the_approved_rerun_still_runs_an_admin_tool_for_an_admin_run():
    rec = _Recorder({"user_x": "admin"})
    part = await _resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=_Manager(rec, ADMIN))
    assert part.error is False and [c[0] for c in rec.calls] == ["user_x"]


# ---- a pause and a failure ------------------------------------------------------------------------------------------------------------------------------


async def test_an_approved_inner_tool_that_pauses_parks_again():
    """The two-phase park: the approval is phase one; the approved tool then pauses the turn for ITS event, which is phase two."""
    blob = {
        "yielded": {
            "resume_metadata": {
                "original_call": {"id": "call-outer", "name": "wait", "arguments": {}},
                "via_call_tool": {"toolset_id": "rec", "tool_name": "wait"},
            },
        },
    }
    with pytest.raises(YieldToWorker) as exc:
        await _resume_tool_approval(blob=blob, payload={"decision": "approved"}, tool_manager=_Manager(_Yielding(), USER))
    assert exc.value.tool_call_id == "call-outer", "the new park answers the call_tool call"
    assert (exc.value.yielded.tool_name, exc.value.yielded.event_key) == ("wait", "k:call-outer"), "it waits on the inner tool's own event"


class _NestedPause(_Yielding):
    """A role-user toolset whose tool pauses INSIDE a nested run, as ``invoke_agent`` does when its child agent pauses: the yield carries the stack of frames."""

    async def call(self, *, tool_name, arguments, principal=None, ctx=None):
        paused = YieldToWorker(Yielded(tool_name="ask_user", event_key=f"ask_user:s-1:child-{ctx.tool_call_id}"), tool_call_id="child-call")
        paused.frames = [AgentFrame(
            agent_id="child", llm_messages=[], tool_call_id=ctx.tool_call_id, depth=1,
            context=AgentResumeContext("s-1", "ws-1", None, "u", []),
        )]
        raise paused


async def test_an_approved_inner_tool_that_pauses_inside_a_nested_run_fails_closed():
    """The coordinator's flat re-park keeps no frames, so the answer to a nested run's question would be delivered to the nested run's call and the
    ``call_tool`` call would never be answered. Such a pause is refused with an error result (what every pause got before this path let one through)
    instead of being parked wrongly."""
    part = await _resume_call_tool_dispatch(
        via={"toolset_id": "rec", "tool_name": "delegate"},
        original_call=ToolCallPart(id="call-outer", name="delegate", arguments={}),
        tool_manager=_Manager(_NestedPause(), USER),
    )
    assert part.error is True and part.id == "call-outer"
    body = json.loads(part.output)
    assert body["is_error"] is True and body["tool_name"] == "delegate" and "nested" in body["reason"], body


async def test_an_inner_error_that_is_not_a_pause_is_still_an_error_result():
    part = await _resume_call_tool_dispatch(
        via={"toolset_id": "rec", "tool_name": "boom"},
        original_call=ToolCallPart(id="call-outer", name="boom", arguments={}),
        tool_manager=_Manager(_Failing(), USER),
    )
    assert part.error is True and part.id == "call-outer"
    body = json.loads(part.output)
    assert body["is_error"] is True and body["tool_name"] == "boom" and "NotFoundError" in body["reason"]


async def test_a_rerun_with_no_registry_or_toolset_fails_closed():
    class _NoRegistry:
        _provider_registry = None

    for manager, via in ((_NoRegistry(), _via()), (_Manager(_Recorder({}), USER), {"tool_name": "user_x"})):
        part = await _resume_call_tool_dispatch(via=via, original_call=CALL, tool_manager=manager)
        assert part.error is True and "call_tool resume" in json.loads(part.output)["reason"]


# ---- the park the handler writes is the park the re-run reads -------------------------------------------------------------------------------------------


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder({"user_x": "user", "admin_x": "admin"})


@pytest.fixture
def system_toolset(sp, pr, recorder: _Recorder):  # noqa: F811 - the re-exported fixtures
    provider = build_system_toolset(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=_DoorRegistry(pr, {"rec": recorder}),  # type: ignore[arg-type]
    )
    pr._system_toolset_provider = provider  # type: ignore[attr-defined]
    return provider


async def _park_through_call_tool(  # noqa: F811
    system_toolset: Any, sp: Any, tool_name: str, initiated_by: PrincipalRef,
) -> dict[str, Any]:
    """Gate ``rec.<tool_name>``, run it through the real ``call_tool`` handler as a run of ``initiated_by``, and return the parked-state blob the worker writes."""
    await sp.get_storage(ToolApprovalPolicy).create(_required_policy("rec", tool_name))
    ctx = ToolContext(tool_call_id="call-outer", session_id="s-1", workspace_id="ws-1", initiated_by=initiated_by)
    with pytest.raises(YieldToWorker) as parked:
        await system_toolset.call(tool_name="call_tool", ctx=ctx, arguments={
            "toolset_id": "rec", "tool_name": tool_name, "arguments": {"a": 1}})
    return {"yielded": parked.value.yielded.to_jsonable()}


def _resumed_by(recorder: _Recorder, initiated_by: PrincipalRef) -> ToolExecutionManager:
    """The manager of the run that resumes the park: a real one, whose registry resolves ``rec``."""
    return ToolExecutionManager.for_workspace(
        toolset_providers={}, session=_Session("s-1", "ws-1"), initiated_by=initiated_by, provider_registry=_Registry(recorder),
    )


async def test_the_park_the_call_tool_handler_writes_is_run_by_the_rerun_as_the_resumed_run(system_toolset, recorder, sp):  # noqa: F811
    blob = await _park_through_call_tool(system_toolset, sp, "user_x", USER)
    assert blob["yielded"]["resume_metadata"]["via_call_tool"] == {"toolset_id": "rec", "tool_name": "user_x"}
    assert recorder.calls == [], "the gate parked the call before the inner tool ran"

    part = await _resume_tool_approval(blob=blob, payload={"decision": "approved"}, tool_manager=_resumed_by(recorder, USER))

    assert part.error is False and part.id == "call-outer"
    ((name, arguments, principal, ctx),) = recorder.calls
    assert (name, arguments, principal) == ("user_x", {"a": 1}, None)
    assert (ctx.tool_call_id, ctx.session_id, ctx.initiated_by) == ("call-outer", "s-1", USER)


async def test_a_park_made_for_an_admin_run_is_not_run_by_a_user_run_that_resumes_it(system_toolset, recorder, sp):  # noqa: F811
    """An admin run may park an admin tool, and the approval is about that call. If the run that resumes it ranks as a user (a forged or stale
    park, or a session whose attribution is not the admin's), approving it does not run the tool: the role is checked against the resumed run."""
    blob = await _park_through_call_tool(system_toolset, sp, "admin_x", ADMIN)
    assert recorder.calls == []

    part = await _resume_tool_approval(blob=blob, payload={"decision": "approved"}, tool_manager=_resumed_by(recorder, USER))

    assert part.error is True and json.loads(part.output)["type"] == "forbidden"
    assert recorder.calls == []
