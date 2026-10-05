"""Run ONE tool call so that a Stop can reach it (stop slice B1).

The loop used to await ``tool_manager.execute`` inline, so a Stop that landed while a call ran could only wait for it:
a command, an MCP call or a subagent ran to its end however many times the operator pressed Stop. :func:`run_stoppable`
runs the call as its OWN task and races it against the Stop event. (Not the same-task scope that
:mod:`primer.agent.interrupt` uses for a model stream: inside the turn task the MCP handshake's uncancel loop would
swallow the Stop's cancel, and neither a grace period nor abandoning a call is possible there.)

The rules, in the order they apply:

* A call that finishes (before the Stop, in the same wake-up, or during the grace) and succeeds gives its REAL result:
  the Stop never throws a real result away.
* "Interrupted" is decided by whether the Stop fired, not by the exception type: an MCP handshake turns a cancel into a
  ConfigError, and an auth error raised after the Stop is a Stop. A park (``YieldToWorker`` / ``ToolWaitPark``) is NOT
  hidden: it propagates, and the loop's handler ends a park that waits on no person as a Stop.
* An interruptible call is cancelled and given :data:`UNWIND_BOUND_S` to unwind, so its own cleanup (an exec's
  process-group kill) and any records it writes land BEFORE the answer is recorded. A call that is not interruptible (a
  file write: cancelling it would release the scope lock while its thread still writes) is not cancelled, only waited
  for, up to :data:`NON_INTERRUPTIBLE_GRACE_S`.
* A call that will not go is ABANDONED: ``on_abandon`` runs first (a subagent's recorder stops accepting its events),
  the task is kept in a strong set (asyncio holds tasks only weakly, so an unreferenced one can be collected mid-flight)
  and a done-callback retrieves and logs its exception.
* A hard Cancel of the turn cancels the call, waits for it (shielded, bounded) so its cleanup has run when the turn task
  ends, and re-raises: a ``CancelledError`` is never swallowed.

The call runs in a COPY of the caller's context (``create_task`` does that); every primer context variable
(``delegation._SINK``, ``invoke._DEPTH``, the node identity, the MCP principal) is set and reset inside one call, so
nothing a call sets is expected back in the turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from primer.model.except_ import AuthRequiredError
from primer.model.yield_ import ToolWaitPark, YieldToWorker

logger = logging.getLogger(__name__)

#: How long a cancelled call gets to unwind (its cleanup, the records it writes) before it is abandoned.
UNWIND_BOUND_S = 2.0

#: How long a call that is NOT interruptible is waited for after a Stop before it is abandoned.
NON_INTERRUPTIBLE_GRACE_S = 5.0

#: Calls given up on. A strong reference: asyncio keeps tasks weakly, so without this an abandoned call could be
#: garbage-collected mid-flight, and its eventual exception would surface as "Task exception was never retrieved".
_ABANDONED: set[asyncio.Task] = set()

T = TypeVar("T")


async def run_stoppable(
    call: Callable[[], Awaitable[T]],
    *,
    interrupt: asyncio.Event,
    interruptible: Callable[[], bool],
    on_abandon: Callable[[], None] | None = None,
    name: str = "tool call",
) -> T | None:
    """Run ``call()`` as its own task; return its result, or None when the Stop fired and there is no usable result.

    ``interruptible`` is asked only once the Stop has fired, so a turn that is never stopped pays nothing for it.
    ``on_abandon`` runs just before a call is abandoned, BEFORE the caller records its own answer. None means "the Stop
    fired and the call did not produce a result": the caller records the synthetic one.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(call())
    task.set_name(f"stoppable:{name}")
    waiter = loop.create_task(interrupt.wait(), name="stoppable:stop-waiter")
    stopped_first = False
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        # Looked at AFTER waking and before anything else can run: a call and a Stop that finish in the same
        # wake-up are a call that finished, and its real result wins.
        stopped_first = not task.done()
        if stopped_first:
            if interruptible():
                task.cancel()
                await asyncio.wait({task}, timeout=UNWIND_BOUND_S)
            else:
                await asyncio.wait({task}, timeout=NON_INTERRUPTIBLE_GRACE_S)
    except asyncio.CancelledError:
        await _unwind_after_a_hard_cancel(task, name)
        raise
    finally:
        waiter.cancel()

    if not task.done():
        _abandon(task, name, on_abandon)
        return None
    return _outcome(task, interrupt, stopped_first=stopped_first, name=name)


def _outcome(task: asyncio.Task, interrupt: asyncio.Event, *, stopped_first: bool, name: str):
    """The finished call's result, or None when it is to be answered as a Stop, or its error as it was inline."""
    if task.cancelled():
        if stopped_first:
            return None
        raise asyncio.CancelledError            # cancelled by something else, as an inline await would have been
    exc = task.exception()
    if exc is None:
        return task.result()
    if isinstance(exc, (YieldToWorker, ToolWaitPark)):
        raise exc
    if stopped_first or (isinstance(exc, AuthRequiredError) and interrupt.is_set()):
        logger.info("tool call %s raised %r after the Stop; answered as interrupted", name, exc)
        return None
    raise exc


async def _unwind_after_a_hard_cancel(task: asyncio.Task, name: str) -> None:
    """The turn task itself is being cancelled: cancel the call and give it a short, SHIELDED wait so its cleanup has run
    (an exec's process-group kill) before the cancellation leaves. A second cancel during the wait is not held up."""
    if not task.done():
        task.cancel()
    try:
        await asyncio.shield(asyncio.wait({task}, timeout=UNWIND_BOUND_S))
    finally:
        if task.done():
            _retrieve(task, name)
        else:
            _abandon(task, name, None)


def _abandon(task: asyncio.Task, name: str, on_abandon: Callable[[], None] | None) -> None:
    if on_abandon is not None:
        try:
            on_abandon()
        except Exception:  # noqa: BLE001 - the hook must never keep the Stop from being answered
            logger.warning("the abandon hook of tool call %s failed", name, exc_info=True)
    logger.warning("tool call %s did not finish after the Stop: abandoned", name)
    _ABANDONED.add(task)
    task.add_done_callback(_retire)


def _retire(task: asyncio.Task) -> None:
    _ABANDONED.discard(task)
    _retrieve(task, task.get_name())


def _retrieve(task: asyncio.Task, name: str) -> None:
    """Mark the exception of a finished call retrieved (and say what it was), so asyncio never reports it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("tool call %s that was given up on ended with %r", name, exc)
