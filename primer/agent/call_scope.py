"""The scope of ONE running tool call, inherited by everything the call starts (stop slice B1).

When a Stop gives up on a call that did not unwind (it is ABANDONED, see :mod:`primer.agent.stoppable_call`), the call keeps
running in the background. What it still emits must not reach the session log after the Stop's answer to the call. A
subagent that itself delegated (``invoke_agent`` inside ``invoke_agent``) tags its events with the INNER call's id, so a
filter on the abandoned call's own id misses it; abandonment has to follow the call's TASK TREE instead.

A :class:`CallScope` is that: a small mutable box, created for a call, bound into the call TASK's context
(:func:`bind_call_scope`, called inside the task, so it is that task's own copy of the context and never leaks into the
turn's), and therefore inherited by every coroutine and task the call starts. Abandoning the call flips the box; the
delegation recorder asks :func:`current_call_scope` and drops what it is handed while the box is flipped. Scopes chain: a
call started inside another call has the outer scope as its parent and is abandoned with it.

This module imports nothing from primer on purpose: both the agent loop side (``stoppable_call``) and the session side
(``delegation``) use it.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio


class CallScope:
    """One tool call's abandonment flag, chained to the scope of the call it runs inside (if any).

    It also carries the Stop event of the turn the call belongs to (``interrupt``), so that what the call starts can stop
    on its own. A cancel of the call's task is not enough: it can land where it is swallowed (the MCP stdio handshake
    clears every pending cancel), and a subagent whose cancel was swallowed would carry on with its next model call and its
    tools after the Stop. A subagent that sees the event ends at its next check, as the turn it runs inside does.
    """

    __slots__ = ("_abandoned", "parent", "interrupt")

    def __init__(self, parent: "CallScope | None" = None, interrupt: "asyncio.Event | None" = None) -> None:
        self.parent = parent
        self.interrupt = interrupt
        self._abandoned = False

    def abandon(self) -> None:
        """The call was given up on: everything it started should stop writing."""
        self._abandoned = True

    @property
    def abandoned(self) -> bool:
        """True once this call, or any call it runs inside, was abandoned."""
        scope: CallScope | None = self
        while scope is not None:
            if scope._abandoned:
                return True
            scope = scope.parent
        return False


_CURRENT: contextvars.ContextVar[CallScope | None] = contextvars.ContextVar("primer_call_scope", default=None)


def current_call_scope() -> CallScope | None:
    """The scope of the tool call this code runs inside, or None outside any call (a turn that is never stopped)."""
    return _CURRENT.get()


def current_interrupt() -> "asyncio.Event | None":
    """The Stop event of the turn the tool call this code runs inside belongs to, or None outside any call.

    The nearest scope that carries one: a call started inside another call has its own scope chained to the outer one, and
    both carry the event of the turn that dispatched them."""
    scope = _CURRENT.get()
    while scope is not None:
        if scope.interrupt is not None:
            return scope.interrupt
        scope = scope.parent
    return None


def bind_call_scope(scope: CallScope) -> None:
    """Bind ``scope`` to the CURRENT context. Call it as the first thing a call's TASK does: the context then belongs to
    that task (a copy made when it was created), is inherited by everything the task starts, and dies with it."""
    _CURRENT.set(scope)


__all__ = ["CallScope", "bind_call_scope", "current_call_scope", "current_interrupt"]
