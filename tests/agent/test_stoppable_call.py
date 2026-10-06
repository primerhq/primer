"""``run_stoppable``: one tool call that a Stop can reach (stop slice B1).

Before, the loop awaited ``tool_manager.execute`` inline, so a Stop that landed while a call ran could only wait for it.
Now the call runs as its own task and the Stop races it. The rules, each pinned below:

* a call that finishes (before the Stop, or in the same wake-up, or during the grace) and succeeds gives its REAL result;
* "interrupted" is decided by whether the Stop fired, not by the exception type (an MCP handshake turns a cancel into a
  ConfigError; an auth error or a park raised after the Stop is a Stop);
* an interruptible call is cancelled and given a bounded wait to unwind, so its own cleanup (the process-group kill) and
  any records it writes land BEFORE the answer; a non-interruptible one (a file write) is not cancelled, only waited for;
* a call that will not go is ABANDONED: a strong reference keeps it from being collected mid-flight, a done-callback
  retrieves its exception, and its scope is flipped before the answer is recorded (the delegation recorder then drops
  whatever the call, and every subagent it started, still emits);
* a hard Cancel of the turn cancels the call, waits for it (shielded, bounded) and re-raises: it is never swallowed.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import logging

import pytest

import primer.agent.stoppable_call as sc
from primer.agent.call_scope import CallScope, current_call_scope
from primer.agent.stoppable_call import run_stoppable
from primer.model.chat import ToolResultPart
from primer.model.except_ import AuthRequiredError, ConfigError
from primer.model.yield_ import Yielded, YieldToWorker


def _yes() -> bool:
    return True


def _no() -> bool:
    return False


def _ok(text: str = "real") -> ToolResultPart:
    return ToolResultPart(id="c1", output=text, error=False)


async def _forever() -> None:
    await asyncio.Event().wait()


@pytest.fixture(autouse=True)
async def _no_leftovers():
    yield
    # every abandoned task is let go and awaited, and no helper task may outlive the test. The wait is BOUNDED, so a test
    # that fails reports its own failure here instead of leaving a task that would hang loop teardown. A call that
    # ignores a cancel only ends when its test releases it, so EVERY such test releases it in a ``finally``; if one is
    # somehow still stubborn here it is dropped from the set and skipped by the checks below, but it is NOT stopped (the
    # loop's shutdown cancelling cannot end a task that swallows cancels, so it outlives the test: that is a bug in the
    # test, which the finally prevents).
    abandoned = list(sc._ABANDONED)
    for task in abandoned:
        task.cancel()
    stubborn: set[asyncio.Task] = set()
    if abandoned:
        _, stubborn = await asyncio.wait(abandoned, timeout=1.0)
    for _ in range(3):
        await asyncio.sleep(0)
    if not stubborn:
        assert not sc._ABANDONED, "an abandoned call was never retired"
    sc._ABANDONED.clear()
    stray = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t not in stubborn]
    assert stray == [], f"tasks left behind: {stray}"


async def test_a_call_that_finishes_without_a_stop_returns_its_result() -> None:
    async def call() -> ToolResultPart:
        await asyncio.sleep(0.01)
        return _ok()

    assert await run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes) == _ok()


async def test_an_error_before_any_stop_propagates_exactly_as_it_did_inline() -> None:
    async def call() -> ToolResultPart:
        raise ConfigError("boom")

    with pytest.raises(ConfigError, match="boom"):
        await run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes)


async def test_a_stop_cancels_an_interruptible_call_and_waits_for_it_to_unwind() -> None:
    """The cleanup (a process-group kill, a delegated record) must have RUN when the answer comes back."""
    cleaned: list[str] = []
    started = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        try:
            await _forever()
        finally:
            await asyncio.sleep(0.1)          # a slow cleanup
            cleaned.append("done")
        return _ok()

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_yes))
    await started.wait()
    interrupt.set()

    assert await run is None
    assert cleaned == ["done"], "the answer came back before the call had unwound"


async def test_the_real_result_wins_when_the_call_and_the_stop_finish_in_the_same_wake_up() -> None:
    interrupt = asyncio.Event()

    async def call() -> ToolResultPart:
        interrupt.set()                        # the Stop lands as the call returns, with no await in between
        return _ok("finished")

    assert await run_stoppable(call, interrupt=interrupt, interruptible=_yes) == _ok("finished")


async def test_a_call_that_finishes_despite_the_cancel_gives_its_real_result() -> None:
    started = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        try:
            await _forever()
        except asyncio.CancelledError:
            return _ok("finished anyway")      # swallowed the cancel and completed within the bound
        return _ok()

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_yes))
    await started.wait()
    interrupt.set()

    assert await run == _ok("finished anyway")


async def test_an_error_raised_after_the_stop_is_a_stop_not_an_error() -> None:
    """The MCP handshake clears every pending cancel and raises ConfigError, so a Stop shows up as an error."""
    started = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        try:
            await _forever()
        except asyncio.CancelledError:
            raise ConfigError("handshake failed") from None
        return _ok()

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_yes))
    await started.wait()
    interrupt.set()

    assert await run is None


def _auth() -> AuthRequiredError:
    return AuthRequiredError("consent", auth_url="https://x", state="s")


async def test_an_auth_error_after_the_stop_is_a_stop() -> None:
    started = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        try:
            await _forever()
        except asyncio.CancelledError:
            raise _auth() from None
        return _ok()

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_yes))
    await started.wait()
    interrupt.set()

    assert await run is None


async def test_an_auth_error_in_the_same_wake_up_as_the_stop_is_a_stop() -> None:
    interrupt = asyncio.Event()

    async def call() -> ToolResultPart:
        interrupt.set()
        raise _auth()

    assert await run_stoppable(call, interrupt=interrupt, interruptible=_yes) is None


async def test_an_auth_error_before_any_stop_still_propagates() -> None:
    async def call() -> ToolResultPart:
        raise _auth()

    with pytest.raises(AuthRequiredError):
        await run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes)


async def test_a_park_after_the_stop_propagates_for_the_loops_stop_handler() -> None:
    """#364's handler in the loop ends a park that waits on no human as a Stop; the helper must not hide it."""
    started = asyncio.Event()
    park = YieldToWorker(Yielded(tool_name="sleep", event_key="timer:c1", resume_metadata={}), tool_call_id="c1")

    async def call() -> ToolResultPart:
        started.set()
        try:
            await _forever()
        except asyncio.CancelledError:
            raise park from None
        return _ok()

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_yes))
    await started.wait()
    interrupt.set()

    with pytest.raises(YieldToWorker) as ei:
        await run
    assert ei.value is park


async def test_a_non_interruptible_call_is_not_cancelled_and_its_real_result_is_used_within_the_grace() -> None:
    """A file write: cancelling it would release the scope lock while the thread still writes."""
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def call() -> ToolResultPart:
        started.set()
        try:
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return _ok("written")

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_no))
    await started.wait()
    interrupt.set()

    assert await run == _ok("written")
    assert cancelled == []


async def test_a_non_interruptible_call_past_the_grace_is_abandoned_with_its_scope_flipped_before_the_answer(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sc, "NON_INTERRUPTIBLE_GRACE_S", 0.1)
    release = asyncio.Event()
    started = asyncio.Event()
    scopes: list[CallScope | None] = []

    async def call() -> ToolResultPart:
        scopes.append(current_call_scope())
        started.set()
        await release.wait()
        return _ok("late")

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_no))
    try:
        await started.wait()
        assert scopes[0] is not None and not scopes[0].abandoned
        interrupt.set()
        answer = await asyncio.wait_for(run, 3.0)

        assert answer is None
        assert scopes[0].abandoned, "the call's scope must be flipped by the time the answer is recorded"
        assert len(sc._ABANDONED) == 1, "an abandoned call needs a strong reference"
    finally:
        release.set()


async def test_an_interruptible_call_that_will_not_unwind_is_abandoned(monkeypatch) -> None:
    monkeypatch.setattr(sc, "UNWIND_BOUND_S", 0.1)
    release = asyncio.Event()
    started = asyncio.Event()
    scopes: list[CallScope | None] = []

    async def call() -> ToolResultPart:
        scopes.append(current_call_scope())
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue                      # ignores the cancel
        return _ok("late")

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_yes))
    try:
        await started.wait()
        interrupt.set()

        assert await asyncio.wait_for(run, 3.0) is None
        assert scopes[0] is not None and scopes[0].abandoned
        assert len(sc._ABANDONED) == 1
    finally:
        release.set()


async def test_an_abandoned_calls_error_is_retrieved_logged_and_its_reference_released(monkeypatch, caplog) -> None:
    """Without the done-callback asyncio logs 'Task exception was never retrieved' (and an unreferenced task can be
    collected mid-flight)."""
    monkeypatch.setattr(sc, "NON_INTERRUPTIBLE_GRACE_S", 0.05)
    caplog.set_level(logging.INFO, logger="primer.agent.stoppable_call")
    loop = asyncio.get_running_loop()
    unretrieved: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    release = asyncio.Event()
    started = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        await release.wait()
        raise RuntimeError("failed after being abandoned")

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_no))
    try:
        await started.wait()
        interrupt.set()
        assert await asyncio.wait_for(run, 3.0) is None
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        gc.collect()
        for _ in range(3):
            await asyncio.sleep(0)

        assert not sc._ABANDONED, "the strong reference was never released"
        assert unretrieved == [], f"asyncio complained: {unretrieved}"
        assert any("failed after being abandoned" in r.getMessage() for r in caplog.records)
    finally:
        release.set()
        loop.set_exception_handler(None)


async def test_a_hard_cancel_cancels_the_call_waits_for_it_and_reraises() -> None:
    """The kill of an exec's process group runs in the call's own cleanup: it must have run when the turn task ends."""
    cleaned: list[str] = []
    started = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        try:
            await _forever()
        finally:
            await asyncio.sleep(0.2)
            cleaned.append("killed")
        return _ok()

    run = asyncio.create_task(run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes))
    await started.wait()
    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run
    assert cleaned == ["killed"], "the CancelledError left before the call's cleanup had run"


async def test_a_hard_cancel_is_reraised_even_when_the_call_ignores_it(monkeypatch) -> None:
    monkeypatch.setattr(sc, "UNWIND_BOUND_S", 0.1)
    release = asyncio.Event()
    started = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return _ok()

    run = asyncio.create_task(run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes))
    try:
        await started.wait()
        run.cancel()

        start = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 3.0)
        assert asyncio.get_running_loop().time() - start < 1.5, "the bounded wait was not bounded"
        assert run.cancelled()
        assert len(sc._ABANDONED) == 1, "the call that would not go is abandoned, not dropped"
    finally:
        release.set()


async def test_a_second_cancel_during_the_shielded_wait_propagates_promptly() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def call() -> ToolResultPart:
        started.set()
        try:
            await _forever()
        except asyncio.CancelledError:
            await release.wait()              # a slow unwind
            raise
        return _ok()

    run = asyncio.create_task(run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes))
    try:
        await started.wait()
        run.cancel()
        await asyncio.sleep(0.05)
        run.cancel()

        start = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 3.0)
        assert asyncio.get_running_loop().time() - start < 0.5, "a second cancel was held up by the shielded wait"
        assert len(sc._ABANDONED) == 1
    finally:
        release.set()


async def test_a_hard_cancel_that_abandons_a_call_flips_its_scope_before_the_cancellation_leaves(monkeypatch) -> None:
    """A worker Cancel (drain, lost lease, operator Cancel) hits the turn while a subagent call runs and does not unwind: the
    call's scope must be flipped on THIS path too (the delegation recorder drops what a flipped scope's tree still emits),
    or the subagent writes to the log after the cancelled turn's terminal record. It is flipped by the time the
    ``CancelledError`` leaves."""
    monkeypatch.setattr(sc, "UNWIND_BOUND_S", 0.1)
    release = asyncio.Event()
    started = asyncio.Event()
    scopes: list[CallScope | None] = []

    async def call() -> ToolResultPart:
        scopes.append(current_call_scope())
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return _ok()

    run = asyncio.create_task(run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes))
    try:
        await started.wait()
        assert scopes[0] is not None and not scopes[0].abandoned
        run.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 3.0)

        assert scopes[0].abandoned, "a call that outlived a hard Cancel was not abandoned"
    finally:
        release.set()


async def test_a_hard_cancel_of_a_call_that_unwinds_in_time_leaves_its_scope_alone() -> None:
    started = asyncio.Event()
    scopes: list[CallScope | None] = []

    async def call() -> ToolResultPart:
        scopes.append(current_call_scope())
        started.set()
        await _forever()
        return _ok()

    run = asyncio.create_task(run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes))
    await started.wait()
    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 3.0)
    assert scopes[0] is not None and not scopes[0].abandoned, "a call that unwound in time was marked abandoned"


async def test_a_scope_never_leaks_into_the_callers_context_even_after_a_call_was_abandoned(monkeypatch) -> None:
    """``run_stoppable`` is awaited INLINE here, in the test task (the loop awaits it in the turn task): the scope must be
    bound inside the call TASK's own copy of the context. If it were bound in the caller's context it would stay there, every
    later call's scope would chain to the abandoned one, and once call 1 was abandoned everything a later call (or the
    next turn in the same task) delegates would be silently dropped."""
    from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink

    class _Writer:
        def __init__(self) -> None:
            self.records: list = []

        async def append(self, rec) -> int:
            self.records.append(rec)
            return len(self.records)

    class _Bus:
        async def publish(self, key, payload) -> None:
            return None

    monkeypatch.setattr(sc, "NON_INTERRUPTIBLE_GRACE_S", 0.1)
    release = asyncio.Event()
    interrupt = asyncio.Event()

    async def call_one() -> ToolResultPart:
        await release.wait()
        return _ok()

    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="s"))
    try:
        asyncio.get_running_loop().call_later(0.05, interrupt.set)
        assert await run_stoppable(call_one, interrupt=interrupt, interruptible=_no) is None      # call 1 is abandoned
        assert current_call_scope() is None, "an abandoned call's scope leaked into the caller's context"

        async def call_two() -> ToolResultPart:
            from primer.model.chat import Done, TextDelta
            from primer.session.delegation import current_delegation_sink

            sink = current_delegation_sink()
            await sink.on_event(TextDelta(index=0, text="from call two"), delegate_tool_call_id="two")
            await sink.on_event(Done(stop_reason="stop", raw_reason="stop"), delegate_tool_call_id="two")
            return _ok()

        await run_stoppable(call_two, interrupt=asyncio.Event(), interruptible=_yes)
        assert writer.records, "a later call's delegated records were dropped: it inherited the abandoned call's scope"
    finally:
        release.set()
        reset_delegation_sink(token)


def test_the_unwind_bound_has_a_margin_over_the_mcp_stdio_termination_timeout() -> None:
    """The MCP SDK's stdio client waits up to ``PROCESS_TERMINATION_TIMEOUT`` for a server to exit once its stdin is closed,
    and only then terminates it. A bound equal to that abandons a cancelled stdio MCP call before its process is reaped,
    so the answer is recorded while the server is still alive (the cleanup lands afterwards). It needs a margin."""
    from mcp.client.stdio import PROCESS_TERMINATION_TIMEOUT

    assert sc.UNWIND_BOUND_S >= PROCESS_TERMINATION_TIMEOUT + 0.5


# --- the scope of a call: abandonment follows the call's task tree --------------------------------------------------------


def test_a_scope_is_abandoned_when_any_enclosing_scope_is() -> None:
    outer = CallScope()
    inner = CallScope(parent=outer)

    assert not outer.abandoned and not inner.abandoned
    inner.abandon()
    assert inner.abandoned and not outer.abandoned, "abandoning an inner call must not abandon the call around it"
    inner2 = CallScope(parent=outer)
    outer.abandon()
    assert inner2.abandoned, "a call inside an abandoned call is abandoned with it"


async def test_a_call_and_everything_it_starts_see_the_scope_and_the_callers_context_never_does(monkeypatch) -> None:
    monkeypatch.setattr(sc, "NON_INTERRUPTIBLE_GRACE_S", 0.1)         # the call below never finishes: abandon it quickly
    seen_in_the_call: list[CallScope | None] = []
    seen_by_a_child: list[bool] = []
    go = asyncio.Event()
    started = asyncio.Event()
    children: list[asyncio.Task] = []

    async def child() -> None:
        await go.wait()
        scope = current_call_scope()
        seen_by_a_child.append(scope is not None and scope.abandoned)

    async def call() -> ToolResultPart:
        seen_in_the_call.append(current_call_scope())
        children.append(asyncio.create_task(child()))          # a task the call starts inherits the scope
        started.set()
        await _forever()
        return _ok()

    interrupt = asyncio.Event()
    run = asyncio.create_task(run_stoppable(call, interrupt=interrupt, interruptible=_no, name="x"))
    await started.wait()
    assert current_call_scope() is None, "the call's scope leaked into the caller's context"
    interrupt.set()
    assert await asyncio.wait_for(run, 3.0) is None

    scope = seen_in_the_call[0]
    assert scope is not None and scope.abandoned, "abandoning the call did not mark its scope"
    go.set()
    await asyncio.wait_for(asyncio.gather(*children), 3.0)
    assert seen_by_a_child == [True], "a task the call started did not see that the call was abandoned"
    assert current_call_scope() is None


async def test_a_call_nested_inside_another_call_has_a_scope_that_follows_its_parents() -> None:
    inner_scopes: list[CallScope | None] = []
    outer_scopes: list[CallScope | None] = []

    async def inner() -> ToolResultPart:
        inner_scopes.append(current_call_scope())
        return _ok()

    async def outer() -> ToolResultPart:
        outer_scopes.append(current_call_scope())
        return await run_stoppable(inner, interrupt=asyncio.Event(), interruptible=_yes)  # type: ignore[return-value]

    await run_stoppable(outer, interrupt=asyncio.Event(), interruptible=_yes)

    assert inner_scopes[0] is not outer_scopes[0], "a nested call must get its own scope"
    outer_scopes[0].abandon()
    assert inner_scopes[0].abandoned, "the nested call's scope does not follow the enclosing call's"


async def test_the_call_runs_in_a_copy_of_the_callers_context() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("b1_probe", default="unset")
    seen: list[str] = []

    async def call() -> ToolResultPart:
        seen.append(var.get())
        var.set("inner")                      # set and reset inside one call, like every primer context variable
        return _ok()

    var.set("turn")
    await run_stoppable(call, interrupt=asyncio.Event(), interruptible=_yes)

    assert seen == ["turn"], "the call did not see the turn's context"
    assert var.get() == "turn", "the call's context leaked back into the turn"
