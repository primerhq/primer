"""Tool-wait batch resume coordinator for the worker pool.

Phase 3 stage 7a (docs/superpowers/2026-08-29-phase3-execution-topology-design.md,
01a0518b). The sibling of :mod:`primer.worker.session_resume_coordinator`
for a session parked on a batch of independently-claimed
``ToolCallTask``s (``parked_state.kind == "tool_wait"``) instead of a
single ``Yielded`` yield. Routed here by
:meth:`primer.worker.pool.WorkerPool._select_resume_handler`, which
peeks the park kind BEFORE ``session_resume_coordinator`` is ever
entered — that module's own rehydration assumes a ``Yielded``-shaped
blob and must stay untouched by this arc (see its own tripwire).

Approved shape (leader ruling, 01a0518b): by the time this runs, every
sibling ``ToolCallTask`` for the parked turn is already terminal (the
last one's ``on_release`` is what re-armed the session's claim lease in
the first place — ruling 2). This coordinator's job is a pure
read-and-materialize step, NOT re-execution:

1. Read all sibling tasks for ``(session_id, turn_no)``, in dispatch
   order.
2. For each gated-then-resumed sibling, write its
   :class:`~primer.model.tool_approval.ToolApprovalRecord` from
   ``gate_state`` (ruling 3's cross-team rider — Wave-3's audit
   surfaces read exclusively from records).
3. Assemble the SAME ``[assistant_tool_use_msg, tool-role Message(N
   ToolResultParts)]`` shape :func:`primer.agent.loop._dispatch_tool_calls`
   already builds for the in-process case, from each sibling's
   ``result_state``.
4. Append that delta via the same persistence seam
   ``session_resume_coordinator.inject_resume_and_continue`` uses
   (single in-process writer, existing lock — the persist-and-handoff
   finding: no new writer concurrency, the atomic-seq question does
   not apply here).
5. Publish the tick, flip ``parked_status`` back to unparked, release
   with ``drop_lease=False`` — the SAME "next claim runs an ordinary
   turn" handoff the session-level resume already uses; no special
   "continue the turn" call is needed.

NOT YET IMPLEMENTED: this requires the tool_wait park-WRITE shape
(what exactly lands in ``session.parked_state`` when
:class:`primer.model.yield_.ToolWaitPark` is caught — the executor
-seam dispatch-loop rewrite, not yet built) to be nailed down first,
since the read side here has to match it exactly. Landing as an
explicit, loud stub rather than silently absent so the routing wiring
in :mod:`primer.worker.pool` and the tripwire in
:mod:`primer.worker.session_resume_coordinator` are independently
reviewable now, ahead of this function's own body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.int.claim import Lease as ClaimLease
    from primer.model.workspace_session import WorkspaceSession
    from primer.worker.pool import WorkerPool


async def resume_engine_tool_wait(
    pool: "WorkerPool", engine_lease: "ClaimLease", session: "WorkspaceSession",
):
    """Drive a tool_wait-parked session's batch to conclusion.

    Placeholder — see module docstring for the approved shape and why
    the body isn't built yet (the park-write shape isn't decided).
    Safe-by-construction in the meantime: nothing in production can
    WRITE a ``parked_state.kind == "tool_wait"`` park until the
    executor-seam dispatch-loop rewrite (which catches
    :class:`~primer.model.yield_.ToolWaitPark` and performs that write)
    exists, so this stub is unreachable today — and loud, not silent,
    if that ever stops being true before this lands.
    """
    raise NotImplementedError(
        f"resume_engine_tool_wait: session {session.id!r} parked with a "
        "tool_wait batch, but the resume coordinator body isn't built yet "
        "(01a0518b executor sub-arc, in progress) - nothing in production "
        "creates a tool_wait park today, so reaching this is itself a bug "
        "if it ever happens before this lands"
    )


__all__ = ["resume_engine_tool_wait"]
