"""``run_stoppable``: one tool call that a Stop can reach (stop slice B1).

Before, the loop awaited ``tool_manager.execute`` inline, so a Stop that landed while a call ran could only wait for it.
Now the call runs as its own task and the Stop races it. The rules, each pinned below:

* a call that finishes (before the Stop, or in the same wake-up, or during the grace) and succeeds gives its REAL result;
* "interrupted" is decided by whether the Stop fired, not by the exception type (an MCP handshake turns a cancel into a
  ConfigError; an auth error or a park raised after the Stop is a Stop);
* an interruptible call is cancelled and given a bounded wait to unwind, so its own cleanup (the process-group kill) and
  any records it writes land BEFORE the answer; a non-interruptible one (a file write) is not cancelled, only waited for;
* a call that will not go is ABANDONED: a strong reference keeps it from being collected mid-flight, a done-callback
  retrieves its exception, and the abandon hook runs before the answer is recorded;
* a hard Cancel of the turn cancels the call, waits for it (shielded, bounded) and re-raises: it is never swallowed.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import logging

import pytest

import primer.agent.stoppable_call as sc
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
    # every abandoned task is let go and awaited, and no helper task may outlive the test
    for task in list(sc._ABANDONED):
        task.cancel()
    await asyncio.gather(*list(sc._ABANDONED), return_exceptions=True)
    for _ in range(3):
        await asyncio.sleep(0)
    assert not sc._ABANDONED, "an abandoned call was never retired"
    stray = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
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


async def test_a_non_interruptible_call_past_the_grace_is_abandoned_with_the_hook_first(monkeypatch) -> None:
    monkeypatch.setattr(sc, "NON_INTERRUPTIBLE_GRACE_S", 0.1)
    release = asyncio.Event()
    started = asyncio.Event()
    order: list[str] = []

    async def call() -> ToolResultPart:
        started.set()
        await release.wait()
        return _ok("late")

    interrupt = asyncio.Event()
    run = asyncio.create_task(
        run_stoppable(call, interrupt=interrupt, interruptible=_no, on_abandon=lambda: order.append("hook")),
    )
    await started.wait()
    interrupt.set()
    answer = await asyncio.wait_for(run, 3.0)
    order.append("answered")

    assert answer is None
    assert order == ["hook", "answered"], "the abandon hook must run before the answer is recorded"
    assert len(sc._ABANDONED) == 1, "an abandoned call needs a strong reference"
    release.set()


async def test_an_interruptible_call_that_will_not_unwind_is_abandoned(monkeypatch) -> None:
    monkeypatch.setattr(sc, "UNWIND_BOUND_S", 0.1)
    release = asyncio.Event()
    started = asyncio.Event()
    hooked: list[bool] = []

    async def call() -> ToolResultPart:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue                      # ignores the cancel
        return _ok("late")

    interrupt = asyncio.Event()
    run = asyncio.create_task(
        run_stoppable(call, interrupt=interrupt, interruptible=_yes, on_abandon=lambda: hooked.append(True)),
    )
    await started.wait()
    interrupt.set()

    assert await asyncio.wait_for(run, 3.0) is None
    assert hooked == [True]
    assert len(sc._ABANDONED) == 1
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
    await started.wait()
    interrupt.set()
    assert await asyncio.wait_for(run, 3.0) is None
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()
    for _ in range(3):
        await asyncio.sleep(0)

    try:
        assert not sc._ABANDONED, "the strong reference was never released"
        assert unretrieved == [], f"asyncio complained: {unretrieved}"
        assert any("failed after being abandoned" in r.getMessage() for r in caplog.records)
    finally:
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
    await started.wait()
    run.cancel()

    start = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 3.0)
    assert asyncio.get_running_loop().time() - start < 1.5, "the bounded wait was not bounded"
    assert run.cancelled()
    assert len(sc._ABANDONED) == 1, "the call that would not go is abandoned, not dropped"
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
    await started.wait()
    run.cancel()
    await asyncio.sleep(0.05)
    run.cancel()

    start = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 3.0)
    assert asyncio.get_running_loop().time() - start < 0.5, "a second cancel was held up by the shielded wait"
    assert len(sc._ABANDONED) == 1
    release.set()


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
