"""The approved re-run of a ``call_tool`` inner tool runs as the resumed run, and the inner role is checked again (pivot P1, task 4).

``system__call_tool`` gates the tool it was asked to run and parks with ``via_call_tool = {toolset_id, tool_name}``. When the gate is approved,
``primer.worker.yield_runtime.resume_call_tool_dispatch`` runs that inner tool. It goes through the helper the ``call_tool`` handler uses,
``primer.agent.inner_dispatch.dispatch_inner_tool``, so that:

* the provider gets the context of the RESUMED run (``tool_manager.context_for``: the session, identity and turn of the manager that is resuming
  it), never a value stored with the park, and a manager that has none gives no context at all;
* a ``principal`` stored in the park (a park from before the pivot carries one a model chose) is never read;
* the inner tool's ``required_role`` is checked against that context's principal again: the approver decided about THIS call, not about who may
  make it, so a stale or forged park cannot run an admin tool for a user-role run;
* a pause of the inner tool (``YieldToWorker``) is not swallowed into an error result. It propagates, and the resume coordinator re-parks the
  session on the tool's own event (``tests/worker/test_approval_yield_repark.py`` pins that half).

A gate raised inside a SUBAGENT (a nested frame) is answered by ``primer.worker.frames.apply_leaf``, which runs the same re-run with the subagent's
resumed manager: the inner tool runs as the subagent, which runs as its parent session, and a pause of it parks the whole chain again
(``Reparked``), the subagent's frame kept. Before this, ``apply_leaf`` sent the park's bare inner tool name to the subagent's manager, which could
not route it, and the whole delegation failed.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.principal import PrincipalRef
from primer.model.tool_approval import ToolApprovalPolicy
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.toolset.system import build_system_toolset
from primer.worker.continuation import Deliver, Repark, resume_continuation
from primer.worker.frames import AgentFrame, AgentResumeContext, Reparked, apply_leaf
from primer.worker.session_resume_coordinator import build_invocation_services
from primer.worker.yield_runtime import _resume_tool_approval, resume_call_tool_dispatch
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
    part = await resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=manager)
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
    part = await resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=manager)
    assert part.error is False
    ((_name, _arguments, principal, ctx),) = rec.calls
    assert principal is None
    assert (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.initiated_by, ctx.turn_no) == ("call-outer", "s-9", "ws-9", USER, 4)


async def test_a_manager_that_cannot_give_a_context_ranks_the_run_as_an_unattributed_user():
    """No context, no identity: the inner provider gets none, an ordinary-user tool runs, and an admin tool is refused (never ranked as the system)."""
    rec = _Recorder({"user_x": "user", "admin_x": "admin"})
    ran = await resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=_NoContextManager(rec))
    assert ran.error is False and rec.calls == [("user_x", {"a": 1}, None, None)]
    refused = await resume_call_tool_dispatch(
        via=_via("admin_x"), original_call=ToolCallPart(id="call-outer", name="admin_x", arguments={}), tool_manager=_NoContextManager(rec),
    )
    assert refused.error is True and json.loads(refused.output)["type"] == "forbidden" and len(rec.calls) == 1


async def test_the_rerun_hands_the_resumed_runs_principal_string_to_the_context():
    """A graph executor dispatches with a principal string (an MCP toolset keys its per-user state on it); the re-run asks the manager for the context with THAT, and the
    provider is handed it. The agent session has none and asks as before (the cases above)."""
    rec = _Recorder({"user_x": "user"})
    manager = _Manager(rec, USER)
    part = await resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=manager, principal="alice")
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
    await resume_call_tool_dispatch(via={"toolset_id": "rec", "principal": "mallory"}, original_call=CALL, tool_manager=manager)
    ((_name, _arguments, principal, ctx),) = rec.calls
    assert principal is None, "the principal an MCP toolset keys its OAuth tokens on is never one stored with the park"
    assert ctx.principal is None
    if not real_manager:
        assert manager.context_requests == [("call-outer", None)]


# ---- the role -----------------------------------------------------------------------------------------------------------------------------------------


async def test_the_approved_rerun_rechecks_the_inner_role():
    rec = _Recorder({"user_x": "admin"})
    part = await resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=_Manager(rec, USER))
    assert part.error is True and part.id == "call-outer"
    assert json.loads(part.output) == {
        "type": "forbidden",
        "message": "tool 'user_x' in toolset 'rec' requires the 'admin' role; this run's principal has the 'user' role",
    }
    assert rec.calls == [], "the approval does not run a tool the resumed run's principal may not run"


async def test_the_approved_rerun_still_runs_an_admin_tool_for_an_admin_run():
    rec = _Recorder({"user_x": "admin"})
    part = await resume_call_tool_dispatch(via=_via(), original_call=CALL, tool_manager=_Manager(rec, ADMIN))
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


def _delegate_park() -> dict[str, Any]:
    """A session's park of a gated ``call_tool`` on ``rec.delegate``, a tool that delegates to a child run."""
    return {
        "yielded": {
            "resume_metadata": {
                "original_call": {"id": "call-outer", "name": "delegate", "arguments": {}},
                "via_call_tool": {"toolset_id": "rec", "tool_name": "delegate"},
            },
        },
    }


async def test_an_approved_inner_tool_that_pauses_inside_a_nested_run_fails_closed_on_the_session_path():
    """The coordinator's flat re-park keeps no frames, so the answer to a nested run's question would be delivered to the nested run's call and the
    ``call_tool`` call would never be answered. On the session path (``_resume_tool_approval``) such a pause is refused with an error result (what
    every pause got before this path let one through) instead of being parked wrongly. The behaviour a fix must reach is pinned by the strict xfail
    in ``tests/worker/test_approval_yield_repark.py``."""
    part = await _resume_tool_approval(blob=_delegate_park(), payload={"decision": "approved"}, tool_manager=_Manager(_NestedPause(), USER))
    assert part.error is True and part.id == "call-outer"
    body = json.loads(part.output)
    assert body["is_error"] is True and body["tool_name"] == "delegate" and "nested" in body["reason"], body


async def test_the_rerun_hands_a_nested_pause_on_with_its_frames():
    """The refusal above belongs to the session path, whose re-park is the one that drops frames. The re-run itself hands the pause on, frames intact,
    so the nested walk (``apply_leaf``), whose re-park keeps every frame, can park the whole chain again."""
    with pytest.raises(YieldToWorker) as paused:
        await resume_call_tool_dispatch(
            via={"toolset_id": "rec", "tool_name": "delegate"},
            original_call=ToolCallPart(id="call-outer", name="delegate", arguments={}),
            tool_manager=_Manager(_NestedPause(), USER),
        )
    assert [frame.tool_call_id for frame in paused.value.frames] == ["call-outer"], "the child's answer goes to the call_tool call"


async def test_an_inner_error_that_is_not_a_pause_is_still_an_error_result():
    part = await resume_call_tool_dispatch(
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
        part = await resume_call_tool_dispatch(via=via, original_call=CALL, tool_manager=manager)
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


# ---- inside a subagent: a nested frame's approval goes through apply_leaf ---------------------------------------------------------------------------

APPROVED = {"decision": "approved"}


def _subagent_services(registry: Any, parent: PrincipalRef | None) -> Any:
    """The services the worker hands the continuation walk to resume session ``s-1`` (``build_invocation_services``), whose run is ``parent``. The
    subagent's manager is the one the real ``build_subagent_toolmanager`` builds over ``registry``."""
    pool = SimpleNamespace(_storage=None, _provider_registry=registry, _approval_resolver=None)
    return build_invocation_services(pool, SimpleNamespace(id="s-1", initiated_by=parent), None, None, None)


def _subagent_frame(initiated_by: PrincipalRef | None = USER) -> AgentFrame:
    """The subagent that called ``system__call_tool``: the session's call ``call-invoke`` started it, and its own ``call_tool`` call (``call-outer``,
    the id the park carries) waits for the approval. ``initiated_by`` is what the frame stored."""
    asked = Message(role="assistant", parts=[ToolCallPart(
        id="call-outer", name="system__call_tool", arguments={"toolset_id": "rec", "tool_name": "user_x", "arguments": {"a": 1}},
    )])
    return AgentFrame(
        agent_id="sub", llm_messages=[asked.model_dump(mode="json")], tool_call_id="call-invoke", depth=1,
        context=AgentResumeContext("s-1", "ws-1", None, None, ["system__call_tool"], initiated_by=initiated_by, turn_no=3),
    )


def _call_tool_leaf(toolset_id: str, tool_name: str, **via: Any) -> Yielded:
    """An approval park of ``system__call_tool`` as the handler writes it, for an inner tool the test's registry holds."""
    return Yielded(tool_name="_approval", event_key="tool_approval:s-1:call-outer", resume_metadata={
        "original_call": {"id": "call-outer", "name": tool_name, "arguments": {"a": 1}},
        "via_call_tool": {"toolset_id": toolset_id, "tool_name": tool_name, **via},
    })


async def test_an_approved_call_tool_gate_inside_a_subagent_runs_the_inner_tool_as_the_subagent(system_toolset, recorder, sp, pr):  # noqa: F811
    """The bug (case S2 of the #787 review): the park the real ``call_tool`` handler wrote, approved in a SUBAGENT's frame. ``apply_leaf`` sent the
    bare inner name to the subagent's manager, which raised ``UnsupportedContentError: unknown tool 'user_x'``, and nothing ran."""
    blob = await _park_through_call_tool(system_toolset, sp, "user_x", USER)

    out = await apply_leaf(
        _subagent_frame(), Yielded.from_jsonable(blob["yielded"]), APPROVED, _subagent_services(_DoorRegistry(pr, {"rec": recorder}), USER),
    )

    assert isinstance(out, ToolResultPart), out
    assert (out.id, out.error, json.loads(out.output)) == ("call-outer", False, {"ran": "user_x"}), "the result answers the subagent's call_tool call"
    ((name, arguments, principal, ctx),) = recorder.calls
    assert (name, arguments, principal) == ("user_x", {"a": 1}, None)
    # The context of the SUBAGENT's resumed manager: its session, workspace and turn, ranked as the session it runs in.
    assert (ctx.tool_call_id, ctx.session_id, ctx.workspace_id, ctx.turn_no, ctx.initiated_by) == ("call-outer", "s-1", "ws-1", 3, USER)


@pytest.mark.parametrize("parent, runs", [(USER, False), (ADMIN, True)], ids=["a-users-session-is-refused", "an-admins-session-runs-it"])
async def test_inside_a_subagent_the_inner_role_is_checked_against_the_session_it_runs_in(  # noqa: F811
    system_toolset, recorder, sp, pr, parent: PrincipalRef, runs: bool,
):
    """A park the handler wrote for an ADMIN run, in a frame that stored that admin, approved and resumed in a session whose run is ``parent``. The
    rank is the session's: nothing stored in the park (the frame's ``initiated_by``, a ``via_call_tool.principal``) is read."""
    blob = await _park_through_call_tool(system_toolset, sp, "admin_x", ADMIN)

    out = await apply_leaf(
        _subagent_frame(initiated_by=ADMIN), Yielded.from_jsonable(blob["yielded"]), APPROVED,
        _subagent_services(_DoorRegistry(pr, {"rec": recorder}), parent),
    )

    if runs:
        assert out.error is False and [c[0] for c in recorder.calls] == ["admin_x"], out.output
    else:
        assert out.error is True and json.loads(out.output)["type"] == "forbidden", out.output
        assert recorder.calls == [], "an admin-only tool ran in a user's session because its park was written for an admin"


async def test_inside_a_subagent_a_legacy_park_principal_is_never_read(system_toolset, recorder, pr):  # noqa: F811
    """A park from before the pivot carries a principal a model chose (and no tool name); the provider's principal is never that value."""
    leaf = _call_tool_leaf("rec", "user_x", principal="mallory")
    del leaf.resume_metadata["via_call_tool"]["tool_name"]

    out = await apply_leaf(_subagent_frame(), leaf, APPROVED, _subagent_services(_DoorRegistry(pr, {"rec": recorder}), USER))

    assert out.error is False, out.output
    ((_name, _arguments, principal, ctx),) = recorder.calls
    assert principal is None and ctx.principal is None


async def test_a_rejected_call_tool_gate_inside_a_subagent_never_runs_the_inner_tool(system_toolset, recorder, sp, pr):  # noqa: F811
    blob = await _park_through_call_tool(system_toolset, sp, "user_x", USER)

    out = await apply_leaf(
        _subagent_frame(), Yielded.from_jsonable(blob["yielded"]), {"decision": "rejected", "reason": "not now"},
        _subagent_services(_DoorRegistry(pr, {"rec": recorder}), USER),
    )

    assert out.error is True and out.id == "call-outer"
    assert json.loads(out.output) == {"rejected": True, "reason": "not now", "tool_name": "user_x", "arguments": {"a": 1}}
    assert recorder.calls == []


async def test_an_inner_tool_that_fails_inside_a_subagent_answers_its_call_with_an_error(system_toolset, pr):  # noqa: F811
    """A failure that is not a pause is the subagent's to read, as the result of its call_tool call: the delegation goes on."""
    out = await apply_leaf(_subagent_frame(), _call_tool_leaf("f", "boom"), APPROVED, _subagent_services(_DoorRegistry(pr, {"f": _Failing()}), USER))

    assert isinstance(out, ToolResultPart) and out.error is True and out.id == "call-outer"
    body = json.loads(out.output)
    assert body["is_error"] is True and body["tool_name"] == "boom" and "NotFoundError" in body["reason"], body


async def test_an_approved_inner_tool_that_pauses_inside_a_subagent_parks_the_chain_again_with_the_subagents_frame(system_toolset, pr):  # noqa: F811
    """The inner tool pauses (as ``ask_user`` does): the leaf is ``Reparked`` under the subagent's call_tool call, and the walk keeps the subagent's
    frame, whose turn has not moved (the same call now waits for the tool's own event)."""
    services = _subagent_services(_DoorRegistry(pr, {"y": _Yielding()}), USER)
    frame = _subagent_frame()

    out = await apply_leaf(frame, _call_tool_leaf("y", "wait"), APPROVED, services)
    walk = await resume_continuation([frame], _call_tool_leaf("y", "wait"), APPROVED, services)

    assert isinstance(out, Reparked), out
    assert (out.new_yield.tool_call_id, out.new_yield.yielded.event_key) == ("call-outer", "k:call-outer")
    assert isinstance(walk, Repark), walk
    assert walk.frames == [frame] and (walk.leaf.tool_name, walk.leaf.event_key) == ("wait", "k:call-outer")


async def test_an_approved_inner_tool_whose_child_run_pauses_inside_a_subagent_parks_with_both_frames(system_toolset, pr):  # noqa: F811
    """The inner tool delegates and its child run pauses (``invoke_agent``'s yield, the child's frame on it). The walk's re-park keeps every frame, so
    unlike the session path this parks the chain again: the subagent's frame, then the child's, whose answer goes to the subagent's call_tool call."""
    walk = await resume_continuation(
        [_subagent_frame()], _call_tool_leaf("n", "delegate"), APPROVED, _subagent_services(_DoorRegistry(pr, {"n": _NestedPause()}), USER),
    )

    assert isinstance(walk, Repark), walk
    assert [(frame.agent_id, frame.tool_call_id) for frame in walk.frames] == [("sub", "call-invoke"), ("child", "call-outer")]
    assert walk.leaf.tool_name == "ask_user"


async def test_the_walk_delivers_the_inner_result_to_the_subagent_and_the_subagents_answer_to_the_session(system_toolset, recorder, sp, pr):  # noqa: F811
    """The whole nested walk (case S2b of the #787 review): the leaf is answered with the inner tool's result, the subagent's turn resumes with it as
    the result of its call_tool call, and the session's call gets the subagent's answer. The walk used to raise before the subagent resumed."""
    blob = await _park_through_call_tool(system_toolset, sp, "user_x", USER)
    resumed: list[dict[str, Any]] = []

    async def resume_subagent(**kw: Any) -> str:
        resumed.append(kw)
        return "done"

    services = dataclasses.replace(_subagent_services(_DoorRegistry(pr, {"rec": recorder}), USER), resume_subagent=resume_subagent)
    frame = _subagent_frame()

    out = await resume_continuation([frame], Yielded.from_jsonable(blob["yielded"]), APPROVED, services)

    assert isinstance(out, Deliver), out
    assert (out.tool_result.id, out.tool_result.error, json.loads(out.tool_result.output)) == ("call-invoke", False, {"output": "done"})
    (kw,) = resumed
    child = kw["child_result"]
    assert (child.id, child.error, json.loads(child.output)) == ("call-outer", False, {"ran": "user_x"})
    assert (kw["agent_id"], kw["invoke_tool_call_id"], kw["llm_messages"]) == ("sub", "call-invoke", frame.llm_messages)
    assert [c[0] for c in recorder.calls] == ["user_x"]
