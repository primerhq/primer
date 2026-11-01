"""L9: a batch made only of read-only, ungated calls may run concurrently behind WorkerConfig.parallel_readonly_tool_calls (default OFF)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from primer.agent.loop import _STOPPED_REFUSAL, _dispatch_tool_calls
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import ToolCallPart, ToolResultPart
from primer.model.except_ import AuthRequiredError, PrimerError


def _call(i: int, name: str = "ts__read") -> ToolCallPart:
    return ToolCallPart(id=f"c{i}", name=name, arguments={"n": i})


class _Manager:
    """A manager double with a timeline: ``start``/``end`` events in the order they happened."""

    def __init__(self, *, flag=False, safe=lambda call: True, latency=0.01, fail=None):
        self.parallel_readonly_tool_calls = flag
        self.timeline: list[tuple[str, str]] = []
        self.safe, self.latency, self.fail = safe, latency, fail or {}

    async def is_parallel_safe(self, call):
        return self.safe(call)

    def is_notifying(self, name):
        return False

    def is_interruptible_call(self, call):
        return True

    async def execute(self, call, *, principal=None):
        self.timeline.append(("start", call.id))
        try:
            await asyncio.sleep(self.latency)
            if call.id in self.fail:
                raise self.fail[call.id]
            return ToolResultPart(id=call.id, output=f"result-{call.id}")
        finally:
            self.timeline.append(("end", call.id))


async def _run(manager, calls, **kw):
    messages = await _dispatch_tool_calls(calls, tool_manager=manager, principal=None, actions_out=[], **kw)
    return [(p.id, p.output, p.error) for m in messages for p in m.parts]


def _overlapped(timeline) -> bool:
    return [k for k, _ in timeline[:5]] == ["start"] * 5


async def test_flag_off_runs_the_batch_one_call_after_another_in_order():
    manager = _Manager(flag=False)
    out = await _run(manager, [_call(i) for i in range(5)])
    assert manager.timeline == [(k, f"c{i}") for i in range(5) for k in ("start", "end")]
    assert [o[0] for o in out] == [f"c{i}" for i in range(5)] and not any(o[2] for o in out)


async def test_flag_on_overlaps_a_fully_read_only_batch_and_keeps_the_result_order():
    manager = _Manager(flag=True)
    out = await _run(manager, [_call(i) for i in range(5)])
    assert _overlapped(manager.timeline)  # all five started before the first finished: one latency unit, not five
    assert [o[:2] for o in out] == [(f"c{i}", f"result-c{i}") for i in range(5)]


async def test_flag_on_but_one_call_not_eligible_runs_serially():
    manager = _Manager(flag=True, safe=lambda call: call.id != "c2")
    await _run(manager, [_call(i) for i in range(5)])
    assert manager.timeline == [(k, f"c{i}") for i in range(5) for k in ("start", "end")]


async def test_a_single_call_is_never_a_parallel_batch():
    manager = _Manager(flag=True)
    await _run(manager, [_call(0)])
    assert manager.timeline == [("start", "c0"), ("end", "c0")]


async def test_a_failing_call_is_answered_as_an_error_result_and_its_siblings_are_unaffected():
    manager = _Manager(flag=True, fail={"c1": PrimerError("boom")})
    out = await _run(manager, [_call(i) for i in range(3)])
    assert out[0][2] is False and out[1][2] is True and "boom" in out[1][1] and out[2][2] is False


async def test_a_sign_in_request_propagates_and_cancels_the_siblings():
    manager = _Manager(flag=True, latency=5.0, fail={})

    async def execute(call, *, principal=None):
        manager.timeline.append(("start", call.id))
        if call.id == "c1":
            raise AuthRequiredError("sign in", auth_url="https://a", state="s")
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            manager.timeline.append(("cancelled", call.id))
            raise

    manager.execute = execute
    with pytest.raises(AuthRequiredError):
        await _run(manager, [_call(i) for i in range(3)])
    assert ("cancelled", "c0") in manager.timeline and ("cancelled", "c2") in manager.timeline


async def test_a_stop_during_a_parallel_batch_answers_every_call_and_records_the_stopped_ones_in_order():
    manager = _Manager(flag=True, latency=5.0)
    interrupt = asyncio.Event()
    stopped: list[ToolCallPart] = []
    task = asyncio.ensure_future(_run(manager, [_call(i) for i in range(3)], interrupt=interrupt, stopped_calls_out=stopped))
    await asyncio.sleep(0.05)
    interrupt.set()
    out = await asyncio.wait_for(task, 2)
    assert [o[0] for o in out] == ["c0", "c1", "c2"] and all(o[2] for o in out)
    assert [c.id for c in stopped] == ["c0", "c1", "c2"]


async def test_a_stop_already_set_answers_the_whole_batch_as_before():
    manager = _Manager(flag=True)
    interrupt = asyncio.Event()
    interrupt.set()
    out = await _run(manager, [_call(i) for i in range(3)], interrupt=interrupt)
    assert manager.timeline == [] and all(o[1] == _STOPPED_REFUSAL and o[2] for o in out)


# ---- the eligibility rule itself ----------------------------------------------------------------


def _real_manager(*, read_only=("ts__read",), notifying=(), uninterruptible=(), yields=False, policy=None):
    manager = ToolExecutionManager.__new__(ToolExecutionManager)
    manager._read_only, manager._notifying, manager._uninterruptible = set(read_only), set(notifying), set(uninterruptible)
    names = {"ts__read", "ts__write"}
    manager._descriptors = {n: SimpleNamespace(yields=yields) for n in names}
    manager._workspace_scoped = {}
    manager._tool_to_toolset = {n: ("ts", n.split("__")[1]) for n in names}

    class _Resolver:
        async def find(self, *, toolset_id, tool_name):
            return policy

    manager._approval_resolver = _Resolver()
    return manager


async def test_only_a_read_only_ungated_interruptible_non_yielding_tool_is_parallel_safe():
    assert await _real_manager().is_parallel_safe(_call(0, "ts__read")) is True
    assert await _real_manager().is_parallel_safe(_call(0, "ts__write")) is False                     # not declared read-only
    assert await _real_manager(notifying=("ts__read",)).is_parallel_safe(_call(0, "ts__read")) is False
    assert await _real_manager(uninterruptible=("ts__read",)).is_parallel_safe(_call(0, "ts__read")) is False
    assert await _real_manager(yields=True).is_parallel_safe(_call(0, "ts__read")) is False
    assert await _real_manager(policy=object()).is_parallel_safe(_call(0, "ts__read")) is False       # an approval policy gates it
    assert await _real_manager().is_parallel_safe(_call(0, "ts__missing")) is False
    assert await _real_manager(read_only=("system__call_tool",)).is_parallel_safe(_call(0, "system__call_tool")) is False


def test_the_flag_defaults_off_everywhere():
    from primer.model.scheduler import WorkerConfig

    assert ToolExecutionManager.parallel_readonly_tool_calls is False
    assert WorkerConfig.model_fields["parallel_readonly_tool_calls"].default is False


def test_the_workspace_read_tools_are_declared_read_only_and_the_mutators_are_not():
    from primer.workspace.local.tools import Edit, Exec, Glob, Grep, Ls, Read, Write
    from primer.workspace.sandbox.tools.glob import SandboxGlob
    from primer.workspace.sandbox.tools.grep import SandboxGrep
    from primer.workspace.sandbox.tools.ls import SandboxLs
    from primer.workspace.sandbox.tools.read import SandboxRead

    for tool in (Ls, Read, Glob, Grep, SandboxLs, SandboxRead, SandboxGlob, SandboxGrep):
        assert tool.read_only is True
    for tool in (Write, Edit, Exec):
        assert tool.read_only is False


async def test_a_workspace_tool_is_checked_against_the_workspace_approval_policy():
    manager = _real_manager(read_only=("workspace__read",), policy=None)
    manager._descriptors["workspace__read"] = SimpleNamespace(yields=False)
    manager._workspace_scoped = {"workspace__read": "read"}
    assert await manager.is_parallel_safe(_call(0, "workspace__read")) is True
    manager_gated = _real_manager(read_only=("workspace__read",), policy=object())
    manager_gated._descriptors["workspace__read"] = SimpleNamespace(yields=False)
    manager_gated._workspace_scoped = {"workspace__read": "read"}
    assert await manager_gated.is_parallel_safe(_call(0, "workspace__read")) is False
