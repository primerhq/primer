"""A subagent cut off where a cancel is swallowed does nothing more after the Stop (stop slice B1 follow-up 01a10e58).

A subagent's own tool calls run inline: ``run_subagent`` runs ``run_agent_turn`` for it WITHOUT the turn's Stop event, so a
Stop reaches it only through the parent's call task being cancelled (``run_stoppable``). If that cancel lands where it is
swallowed, nothing else stops the subagent. The place found in review of #373 is the MCP stdio handshake
(``McpToolsetProvider._enter_stdio_session``): a cancel during it clears every pending cancel and raises ``ConfigError``,
``ToolExecutionManager`` turns that into an error result, and the subagent's loop carries on with its next model call and
its next tools (an ``exec``, a write) after the operator pressed Stop. The parent abandons the call and hides its records
(``CallScope``), but the side effects are real.

This drives the real pieces end to end: the REAL stdio provider (``call`` -> ``_open_session`` -> ``_enter_stdio_session``)
pointed at a launched process that is not an MCP server (so the handshake never completes), the real ``run_subagent``
and ``run_agent_turn``, the real ``ToolExecutionManager`` and the real ``run_stoppable``. Only the model is scripted: the
round after the handshake call asks for an ``exec`` tool that records that it ran.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

# Importing primer.workspace makes ToolCallContext.model_rebuild() run (the forward reference to AgentSession).
import primer.workspace  # noqa: F401
import primer.agent.invoke as invoke
import primer.agent.stoppable_call as sc
from primer.agent.invoke import run_subagent
from primer.agent.stoppable_call import run_stoppable
from primer.agent.tool_manager import ToolExecutionManager
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, TextDelta, Tool, ToolCallEnd, ToolCallResult, ToolCallStart
from primer.model.model_profile import ModelProfileConfig
from primer.model.principal import PrincipalRef
from primer.model.provider import McpConfig, StdioConfig, TransportType
from primer.model_profile import ResolvedModel
from primer.toolset.mcp import McpToolsetProvider

AGENT = Agent(id="sub", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10)
MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
)
_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class _ScriptedLlm:
    """Round N asks for ``rounds[N]`` (a list of (call id, tool name)); ``None`` is the final answer. A request past the end
    is answered with the final answer, so a subagent that is not stopped finishes by itself instead of hanging."""

    def __init__(self, rounds: list[list[tuple[str, str]] | None], *, first_round_text: str | None = None) -> None:
        self.rounds = rounds
        self.requests = 0
        # Text the model streams in round 1 BEFORE its tool calls: a subagent that was stopped after it had already said
        # something (so that it has a "partial answer" the parent must not be handed as a result).
        self.first_round_text = first_round_text

    def stream(self, **_kwargs: Any):
        self.requests += 1
        calls = self.rounds[min(self.requests, len(self.rounds)) - 1]
        lead = self.first_round_text if self.requests == 1 else None

        async def gen():
            if calls is None:
                yield TextDelta(text="all done", index=0)
                yield Done(stop_reason="stop", raw_reason="stop")
                return
            if lead:
                yield TextDelta(text=lead, index=0)
            for index, (call_id, name) in enumerate(calls, start=1 if lead else 0):
                yield ToolCallStart(id=call_id, name=name, index=index)
                yield ToolCallEnd(id=call_id, arguments={}, index=index)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return gen()


class _HandshakeNeverCompletes(McpToolsetProvider):
    """The REAL stdio provider; only the catalogue is fixed (listing tools would itself open a session and hang)."""

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(
            id="hang", description="a stdio MCP tool whose server never answers the handshake",
            toolset_id=self._toolset_id, args_schema=_SCHEMA,
        )


class _Recorder:
    """A toolset whose one tool records that it ran: the side effect a stopped subagent must not have.

    ``on_call`` runs inside the tool call (for a test where the Stop lands DURING a call that then finishes)."""

    def __init__(self, tool_id: str = "exec", on_call: Callable[[], None] | None = None) -> None:
        self.executed: list[str] = []
        self.tool_id = tool_id
        self.on_call = on_call

    def required_role(self, tool_name: str) -> str:
        return "user"

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id=self.tool_id, description="runs a command", toolset_id="rec", args_schema=_SCHEMA)

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal: str | None = None, ctx=None):
        self.executed.append(tool_name)
        if self.on_call is not None:
            self.on_call()
        return ToolCallResult(output="ran", is_error=False)


def _marked_pids(marker: str) -> list[int]:
    """The processes whose command line carries ``marker`` (the launched fake MCP server), found without a shell."""
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                if marker.encode() in handle.read():
                    found.append(int(entry))
        except OSError:
            continue
    return found


def _kill_marked(marker: str) -> None:
    for pid in _marked_pids(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def _until(condition: Callable[[], Any], within: float = 15.0) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the condition never became true")


@pytest.fixture(autouse=True)
async def _no_abandoned_leftovers():
    yield
    abandoned = list(sc._ABANDONED)
    for task in abandoned:
        task.cancel()
    if abandoned:
        await asyncio.wait(abandoned, timeout=3.0)
    sc._ABANDONED.clear()


async def test_a_calls_scope_carries_the_turns_stop_event_to_everything_the_call_starts() -> None:
    """How the subagent gets the Stop: ``run_stoppable`` binds the turn's event into the call's scope, so the code the call
    runs, and the tasks that code starts, can see it (``current_interrupt``), and code outside any call sees none."""
    from primer.agent.call_scope import current_interrupt

    stop = asyncio.Event()
    seen: list[asyncio.Event | None] = [current_interrupt()]

    async def call():
        seen.append(current_interrupt())

        async def inner() -> None:
            seen.append(current_interrupt())

        await asyncio.create_task(inner())
        return "real"

    assert await run_stoppable(call, interrupt=stop, interruptible=lambda: True) == "real"

    assert seen == [None, stop, stop]
    assert current_interrupt() is None, "the event leaked out of the call's own context"


async def test_a_scope_without_an_event_inside_one_that_has_it_still_finds_the_outer_event() -> None:
    """``current_interrupt`` walks up the chain. A scope that carries no event of its own (what any scope that
    ``run_stoppable`` did not make looks like) bound inside one that does must not hide it: reading only the innermost
    scope would give a subagent started there no Stop event at all."""
    from primer.agent.call_scope import CallScope, bind_call_scope, current_call_scope, current_interrupt

    outer_stop = asyncio.Event()

    async def call():
        bind_call_scope(CallScope(parent=current_call_scope()))      # no ``interrupt``: a child scope with no event
        return current_interrupt()

    assert await run_stoppable(call, interrupt=outer_stop, interruptible=lambda: True) is outer_stop


async def test_a_nested_call_sees_its_own_events_scope_and_the_outer_one_is_restored() -> None:
    from primer.agent.call_scope import current_interrupt

    outer_stop, inner_stop = asyncio.Event(), asyncio.Event()
    seen: list[asyncio.Event | None] = []

    async def inner():
        seen.append(current_interrupt())
        return "inner"

    async def outer():
        seen.append(current_interrupt())
        await run_stoppable(inner, interrupt=inner_stop, interruptible=lambda: True)
        seen.append(current_interrupt())
        return "outer"

    assert await run_stoppable(outer, interrupt=outer_stop, interruptible=lambda: True) == "outer"

    assert seen == [outer_stop, inner_stop, outer_stop]


async def test_a_call_that_ends_cancelled_in_the_same_wake_up_as_the_stop_is_answered_as_stopped() -> None:
    """A subagent that has the Stop event ends cancelled on its own, so it can finish in the SAME wake-up as the Stop (before
    ``run_stoppable`` has looked at whether the call was still running). That is a stopped call, not a call cancelled by
    something else: the second reading re-raised ``CancelledError`` into the PARENT's dispatch, which the worker takes for a
    hard Cancel of the whole turn."""
    stop = asyncio.Event()

    async def call():
        await stop.wait()
        raise asyncio.CancelledError("subagent stopped by user")

    runner = asyncio.create_task(run_stoppable(call, interrupt=stop, interruptible=lambda: True, name="invoke_agent"))
    await asyncio.sleep(0.05)                                # the call is waiting for the Stop
    stop.set()

    assert await asyncio.wait_for(runner, timeout=5.0) is None


async def test_a_call_cancelled_by_something_else_while_no_stop_is_set_still_raises() -> None:
    """The other half of that rule: with no Stop, a call that ends cancelled was cancelled by something else, and the
    cancellation is not turned into an answer."""
    stop = asyncio.Event()

    async def call():
        raise asyncio.CancelledError("cancelled from elsewhere")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_stoppable(call, interrupt=stop, interruptible=lambda: True, name="x"), timeout=5.0)


async def test_a_subagent_outside_any_call_is_given_no_stop_event(monkeypatch) -> None:
    """Nothing changes for a subagent that is not run under ``run_stoppable`` (a turn that is never stopped): it gets no
    event and finishes normally."""
    llm = _ScriptedLlm([None])
    manager = ToolExecutionManager(
        toolset_providers={"rec": _Recorder()},  # type: ignore[arg-type]
        initiated_by=PrincipalRef(type="user", id="u1", display="u1", role="user", source="local"),
    )

    async def resolve(agent_id, *, storage_provider, provider_registry):
        return AGENT, llm, MODEL

    async def build(context, *, storage_provider=None, provider_registry=None, approval_resolver=None):
        return manager

    monkeypatch.setattr(invoke, "_resolve_agent_runtime", resolve)
    monkeypatch.setattr(invoke, "build_subagent_toolmanager", build)

    text = await run_subagent(agent_id="sub", prompt="go", storage_provider=None, provider_registry=None)

    assert text == "all done" and llm.requests == 1


async def test_a_subagent_that_ends_on_the_stop_check_does_not_return_what_it_had_produced(monkeypatch) -> None:
    """``run_subagent`` returns ``_final_assistant_text(produced)`` for a turn that ENDED. A turn that ended because the
    Stop was set has produced something (here: round 1's assistant message, text plus a fast tool call), and returning it
    would hand the parent a stopped subagent's partial answer as the call's REAL result (a call that finishes after the
    Stop with a value is used as it is). It must raise instead, so the call ends cancelled and is answered "interrupted".

    The Stop lands DURING the tool call, which then finishes (a call that finishes in the same wake-up as the Stop keeps its
    real result), so the subagent reaches the loop's top-of-round Stop check with no cancel arriving: that check, and not a
    cancel propagating through the subagent's own ``run_stoppable``, is what ends the turn. The call runs in a task that is
    bound to a scope carrying the event, as ``run_stoppable`` would, but nobody cancels it, so what ``run_subagent`` does
    with an ended-by-the-Stop turn is the only thing under test."""
    from primer.agent.call_scope import CallScope, bind_call_scope

    stop = asyncio.Event()
    recorder = _Recorder(tool_id="quick", on_call=stop.set)
    llm = _ScriptedLlm([[("c1", "rec__quick")], [("c2", "rec__quick")], None], first_round_text="I checked the repository")
    manager = ToolExecutionManager(
        toolset_providers={"rec": recorder},  # type: ignore[arg-type]
        initiated_by=PrincipalRef(type="user", id="u1", display="u1", role="user", source="local"),
    )

    async def resolve(agent_id, *, storage_provider, provider_registry):
        return AGENT, llm, MODEL

    async def build(context, *, storage_provider=None, provider_registry=None, approval_resolver=None):
        return manager

    monkeypatch.setattr(invoke, "_resolve_agent_runtime", resolve)
    monkeypatch.setattr(invoke, "build_subagent_toolmanager", build)

    async def in_a_call():
        bind_call_scope(CallScope(interrupt=stop))                   # inside the task: its own copy of the context
        return await run_subagent(agent_id="sub", prompt="go", storage_provider=None, provider_registry=None)

    task = asyncio.create_task(in_a_call())
    done, pending = await asyncio.wait({task}, timeout=10.0)
    try:
        assert not pending, "the subagent never ended"
        outcome = "was cancelled" if task.cancelled() else f"returned {task.result()!r}"
        assert task.cancelled(), f"the stopped subagent {outcome}: its partial text would reach the parent as a result"
        assert recorder.executed == ["quick"], f"only the call the Stop landed in may have run: {recorder.executed}"
        assert llm.requests == 1, f"the subagent asked the model again after the Stop: {llm.requests} requests"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_subagent_cut_off_in_the_stdio_handshake_does_nothing_more_after_the_stop(monkeypatch) -> None:
    """End to end through the REAL stdio provider. Which path ends the subagent here is worth knowing: the parent's cancel
    reaches the subagent through ITS OWN ``run_stoppable`` (its tool call now runs in a task, because it has the event), so
    the subagent ends by that cancel propagating, not by the loop's Stop check. Before this change the cancel was swallowed
    inside the handshake and the subagent went on to its next model call. What ends a turn on the Stop check instead (and
    must not return its text) is pinned by ``test_a_subagent_that_ends_on_the_stop_check_...`` above."""
    marker = f"primer-handshake-{uuid.uuid4().hex}"
    llm = _ScriptedLlm([[("c1", "ext__hang")], [("c2", "rec__exec")], None])
    recorder = _Recorder()
    provider = _HandshakeNeverCompletes(
        toolset_id="ext",
        config=McpConfig(
            transport=TransportType.STDIO,
            # Not an MCP server: it starts, reads nothing and never answers ``initialize``.
            config=StdioConfig(command=[sys.executable, "-c", f"import time; time.sleep(120)  # {marker}"]),
        ),
    )
    manager = ToolExecutionManager(
        toolset_providers={"ext": provider, "rec": recorder},  # type: ignore[arg-type]
        initiated_by=PrincipalRef(type="user", id="u1", display="u1", role="user", source="local"),
    )

    async def resolve(agent_id, *, storage_provider, provider_registry):
        return AGENT, llm, MODEL

    async def build(context, *, storage_provider=None, provider_registry=None, approval_resolver=None):
        return manager

    monkeypatch.setattr(invoke, "_resolve_agent_runtime", resolve)
    monkeypatch.setattr(invoke, "build_subagent_toolmanager", build)
    stop = asyncio.Event()
    call = asyncio.create_task(run_stoppable(
        lambda: run_subagent(agent_id="sub", prompt="go", storage_provider=None, provider_registry=None),
        interrupt=stop, interruptible=lambda: True, name="invoke_agent",
    ))
    try:
        await _until(lambda: _marked_pids(marker) or call.done())    # the server process exists: handshake in flight
        assert not call.done(), f"the subagent ended before reaching the handshake: {call.exception()!r}"
        await asyncio.sleep(0.3)
        assert llm.requests == 1 and recorder.executed == [], "the subagent was not inside the handshake: not the situation"

        stop.set()
        result = await asyncio.wait_for(call, timeout=30.0)
        await asyncio.sleep(0.5)                              # whatever the subagent still does after the parent's answer

        assert llm.requests == 1, f"the subagent made {llm.requests - 1} more model call(s) after the Stop"
        assert recorder.executed == [], f"the subagent ran {recorder.executed} after the Stop"
        # None is "the Stop fired and the call has no usable result": the parent answers it ``interrupted: stopped by
        # user``. A text here would be a stopped subagent's partial answer presented to the model as a real result.
        assert result is None, f"the parent was handed {result!r} as the call's result"
    finally:
        stop.set()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        _kill_marked(marker)
