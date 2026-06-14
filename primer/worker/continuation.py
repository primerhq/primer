"""Pure continuation-walk for the unified nested-yield resume flow.

When a yield (an approval gate or a yielding tool) is raised inside a
*nested* invocation, the worker parks the whole chain of in-flight callers
as a :mod:`primer.worker.frames` stack plus a leaf yield. On resume, this
module owns the **pure walk** that:

1. resolves the leaf at the innermost frame's tool context
   (:func:`primer.worker.frames.apply_leaf`), then
2. unwinds the frame stack innermost -> outermost, threading each frame's
   completed result up into its parent as the resolved child result.

The walk produces exactly one of two outcomes:

* :class:`Deliver` - the chain finished cleanly; its single
  ``tool_result`` is the value the *session's* pending tool call resolves
  to (the worker injects it and re-runs the session turn).
* :class:`Repark` - a frame (or the leaf re-dispatch) raised a fresh
  yield mid-unwind; the worker must re-park with the reconstructed frame
  stack + new leaf.

This module is deliberately I/O-free and pool-free: it takes the frames,
the leaf, the resume payload, and a small :class:`InvocationServices`
bundle of callables (bound to real implementations by the pool wiring in
Task 3.3b). The frames themselves reach those callables via
``services.<name>(...)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from primer.model.chat import ToolResultPart
from primer.worker.frames import apply_leaf


# ===========================================================================
# Services bundle
# ===========================================================================


@dataclass
class InvocationServices:
    """Callables the frames need to rehydrate + drive nested invocations.

    A thin carrier so the pure walk (and the frames it drives) can reach the
    real rehydration/resume implementations without importing pool/storage.
    Task 3.3b binds these to concrete implementations; the walk only ever
    calls them as ``services.<name>(...)``.

    Attributes
    ----------
    build_subagent_toolmanager
        Build a tool manager for a subagent from its
        :class:`~primer.worker.frames.AgentResumeContext` (used by
        :func:`~primer.worker.frames.apply_leaf` to re-dispatch an approved
        gated tool).
    resume_subagent
        Rehydrate + re-run a parked subagent turn with a resolved child
        result; returns the subagent's final text or re-raises
        :class:`~primer.model.yield_.YieldToWorker`.
    resolve_graph
        Resolve a graph id to its graph definition.
    build_child_graph_executor
        Build a child graph executor from a resolved graph + child
        graph-session id, for resuming a parked :class:`GraphFrame`.
    """

    build_subagent_toolmanager: Callable[..., Any]
    resume_subagent: Callable[..., Any]
    resolve_graph: Callable[..., Any]
    build_child_graph_executor: Callable[..., Any]


# ===========================================================================
# Walk outcomes
# ===========================================================================


@dataclass
class Deliver:
    """The continuation finished cleanly.

    ``tool_result`` is the final :class:`~primer.model.chat.ToolResultPart`
    the worker injects into the SESSION's pending tool call before re-running
    the session turn.
    """

    tool_result: ToolResultPart


@dataclass
class Repark:
    """The continuation raised a fresh yield and must be re-parked.

    ``frames`` is the reconstructed stack (root-first) the worker re-parks,
    and ``leaf`` is the new innermost yield to await.
    """

    frames: list
    leaf: Any


# ===========================================================================
# The walk
# ===========================================================================


async def resume_continuation(
    frames: list,
    leaf: Any,
    payload: Any,
    services: InvocationServices,
) -> Deliver | Repark:
    """Resolve ``leaf`` at the innermost frame, then unwind the stack.

    ``frames`` MUST be non-empty (the worker resolves the empty-frames case -
    a session that yielded directly with no nesting - via its own path; this
    walk only handles nested continuations).

    Returns :class:`Deliver` when the whole chain completes, or
    :class:`Repark` when the leaf re-dispatch or any frame's resume raises a
    fresh yield mid-unwind. On a mid-unwind repark the reconstructed stack
    keeps the frames *outer* of the reparking frame and appends the new
    yield's own nested stack (``ny.frames``); the new leaf is ``ny.yielded``.
    """
    assert frames, "resume_continuation requires a non-empty frame stack"

    # 1. Resolve the leaf at the innermost frame.
    result = await apply_leaf(frames[-1], leaf, payload, services)
    if not isinstance(result, ToolResultPart):  # it's a Reparked
        ny = result.new_yield
        return Repark(frames=list(frames[:-1]) + list(ny.frames or []), leaf=ny.yielded)

    # 2. Unwind innermost -> outermost.
    for i in range(len(frames) - 1, -1, -1):
        outcome = await frames[i].resume(result, services)
        if not outcome.completed:  # Reparked
            ny = outcome.new_yield
            return Repark(frames=list(frames[:i]) + list(ny.frames or []), leaf=ny.yielded)
        result = outcome.value

    return Deliver(tool_result=result)


__all__ = [
    "Deliver",
    "InvocationServices",
    "Repark",
    "resume_continuation",
]
