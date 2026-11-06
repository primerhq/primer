"""Pure continuation-walk for the unified nested-yield resume flow.

When a yield (an approval gate or a yielding tool) is raised inside a
*nested* invocation, the worker parks the whole chain of in-flight callers
as a :mod:`primer.worker.frames` stack plus a leaf yield. On resume, this
module owns the **pure walk** that:

1. lets the innermost frame resolve its OWN leaf polymorphically
   (:meth:`primer.worker.frames.AgentFrame.resume_leaf` via ``apply_leaf``,
   :meth:`primer.worker.frames.GraphFrame.resume_leaf` via the child graph's
   own resume), then
2. unwinds the rest of the frame stack innermost -> outermost, threading each
   frame's completed result up into its parent as the resolved child result.

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

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from primer.model.chat import ToolResultPart


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
    graph_agent_tool_result
        Compute the ``agent_tool_result`` Message an agent-node leaf inside a
        child graph continues from (from the child checkpoint + tool_call_id +
        raw payload), or ``None`` for approval/verdict leaves. Used by
        :meth:`~primer.worker.frames.GraphFrame.resume_leaf`.
    session_id
        The session being resumed, for the ``ResumeContext`` a value-yielding
        ``tool_call`` node inside a child graph hands its resume hook. Used by
        :meth:`~primer.worker.frames.GraphFrame.resume_leaf`. ``None`` for a
        bundle built without a session.
    resolve_provider
        The provider registry's ``get_toolset``, for the same context (a python
        toolset's hook reaches its provider through it). ``None`` where the
        worker has no registry.
    """

    build_subagent_toolmanager: Callable[..., Any]
    resume_subagent: Callable[..., Any]
    resolve_graph: Callable[..., Any]
    build_child_graph_executor: Callable[..., Any]
    graph_agent_tool_result: Callable[..., Any]
    session_id: str | None = None
    resolve_provider: Callable[[str], Awaitable[Any]] | None = None


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
    ``leaf`` is the new innermost yield to await, and ``tool_call_id`` is the
    exact pending call that raised that leaf.
    """

    frames: list
    leaf: Any
    tool_call_id: str | None = None


# ===========================================================================
# The walk
# ===========================================================================


async def resume_continuation(
    frames: list,
    leaf: Any,
    payload: Any,
    services: InvocationServices,
    fired_key: str | None = None,
    gate_id: str | None = None,
    leaf_tool_call_id: str | None = None,
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

    ``fired_key`` is the event key the reply FIRED (the parked row's ``resume_event_key``). An agent session parked on an ``invoke_graph`` child offers every
    pending gate of the child, but ``leaf`` is the child's PRIMARY projection, so a reply for a non-primary sibling must select the entry it names, not the
    leaf (C-033 round 3, ticket 01a11fc6-0cce). A frame whose leaf is exact (an agent frame) ignores it.

    ``gate_id`` is the gate the decision named, read from the RAW wake (the ``payload`` here is the classified one, whose ``__yield_gate_id__`` is stripped). A child graph's sibling gates can share the key and
    a gated inner call runs with the approval bypassed, so the child resumes only the entry of that gate (security review of #724, round 3, B1-r2a). It is handed on only when there is one.

    ``leaf_tool_call_id`` is the exact pending call the parked leaf belongs to.
    It differs from an :class:`~primer.worker.frames.AgentFrame`'s own
    ``tool_call_id``, which names the parent's ``invoke_agent`` call.
    """
    assert frames, "resume_continuation requires a non-empty frame stack"

    # 1. The innermost frame resolves its OWN leaf polymorphically: an
    #    AgentFrame defers to apply_leaf (then continues its subagent turn),
    #    a GraphFrame defers to the child graph's own resume. Either returns a
    #    Completed (the leaf answer / final value) or a Reparked.
    inner = frames[-1]
    leaf_kwargs = {"fired_key": fired_key}
    if gate_id is not None:
        leaf_kwargs["gate_id"] = gate_id
    if leaf_tool_call_id is not None:
        leaf_kwargs["leaf_tool_call_id"] = leaf_tool_call_id
    outcome = await inner.resume_leaf(leaf, payload, services, **leaf_kwargs)
    if not outcome.completed:  # Reparked
        ny = outcome.new_yield
        return Repark(
            frames=list(frames[:-1]) + list(ny.frames or []),
            leaf=ny.yielded,
            tool_call_id=getattr(ny, "tool_call_id", None),
        )
    result = outcome.value

    # 2. Unwind the REST: innermost-but-one -> outermost.
    for i in range(len(frames) - 2, -1, -1):
        outcome = await frames[i].resume(result, services)
        if not outcome.completed:  # Reparked
            ny = outcome.new_yield
            return Repark(
                frames=list(frames[:i]) + list(ny.frames or []),
                leaf=ny.yielded,
                tool_call_id=getattr(ny, "tool_call_id", None),
            )
        result = outcome.value

    return Deliver(tool_result=result)


async def resume_pending_agent_yield(
    entry: dict[str, Any],
    payload: Any,
    services: InvocationServices,
    *,
    fired_key: str | None = None,
    gate_id: str | None = None,
) -> Deliver | Repark:
    """Resume the nested continuation carried by one graph agent entry.

    Graph sessions and invoked child graphs persist the same
    ``pending_agent_yields`` shape.  Keeping its frame/leaf decoding and walk
    in one helper prevents the child-graph path from bypassing the nested
    subagent before it resumes the graph node.
    """
    from primer.model.yield_ import Yielded
    from primer.worker.frames import frames_from_jsonable

    frames = frames_from_jsonable(list(entry.get("frames") or []))
    leaf_raw = entry.get("leaf")
    assert frames and leaf_raw, "nested pending agent yield requires frames and leaf"
    return await resume_continuation(
        frames,
        Yielded.from_jsonable(leaf_raw),
        payload,
        services,
        fired_key=fired_key,
        leaf_tool_call_id=entry.get("tool_call_id"),
        **({"gate_id": gate_id} if gate_id is not None else {}),
    )


def checkpoint_with_reparked_agent_yield(
    checkpoint: dict[str, Any],
    entry: dict[str, Any],
    outcome: Repark,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return ``checkpoint`` with ``entry`` advanced to ``outcome``.

    The graph itself has not advanced when the nested continuation re-yields;
    only the selected pending entry's frame stack, leaf and wait metadata do.
    Matching by node plus the old event key preserves sibling selection when
    provider call ids collide.
    """
    from primer.model.yield_ import wait_deadline
    from primer.worker.frames import frames_to_jsonable

    leaf = outcome.leaf
    stamped_at = now or datetime.now(timezone.utc)
    updated = deepcopy(checkpoint)
    for pending in updated.get("pending_agent_yields") or []:
        if (
            pending.get("node_id") == entry.get("node_id")
            and pending.get("event_key") == entry.get("event_key")
        ):
            pending["frames"] = frames_to_jsonable(list(outcome.frames))
            pending["leaf"] = leaf.to_jsonable()
            pending["event_key"] = leaf.event_key
            pending["tool_name"] = leaf.tool_name
            pending["resume_metadata"] = dict(leaf.resume_metadata or {})
            if outcome.tool_call_id is not None:
                pending["tool_call_id"] = outcome.tool_call_id
            pending["deadline"] = wait_deadline(leaf.timeout, stamped_at)
            break
    else:
        raise RuntimeError("the nested graph entry being resumed is no longer pending")
    return updated


__all__ = [
    "checkpoint_with_reparked_agent_yield",
    "Deliver",
    "InvocationServices",
    "Repark",
    "resume_continuation",
    "resume_pending_agent_yield",
]
