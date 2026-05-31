"""Worker-side adapter for resuming a graph parked at a ToolCall approval.

Spec B Phase 6 / Phase 11. When a graph-bound session yields for tool
approval (a ``_ToolCallNode`` whose underlying tool tripped the
approval gate), the worker writes the graph executor's checkpoint into
:attr:`ParkedState.graph_checkpoint`. On resume, the worker can't
re-enter the agent ``inject_resume_messages`` path because graph
sessions have no per-turn LLM history surface — they instead expose
:meth:`Graph.resume_from_checkpoint` which drains the pending
ToolCalls with ``bypass_approval=True``.

This module owns that dispatch:

* :func:`resume_graph_from_checkpoint` — given a fresh
  :class:`WorkspaceGraphExecutor` already wired with the same
  agent / tool / state resolvers, plus the JSON-able snapshot and the
  classified resume payload (approved / rejected / timeout / cancel),
  drain the executor's resume stream to completion.

The adapter is intentionally tiny so the worker pool can call it from
a single ``if`` branch in :meth:`WorkerPool._handle_resume`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from primer.model.yield_ import YieldCancelled, YieldTimeout


if TYPE_CHECKING:
    from primer.graph.workspace_executor import WorkspaceGraphExecutor


logger = logging.getLogger(__name__)


def _decision_from_payload(
    payload: "dict[str, Any] | YieldTimeout | YieldCancelled | Any",
) -> tuple[str, str | None]:
    """Classify the resume payload into (decision, reason).

    Mirrors :func:`primer.worker.yield_runtime._resume_tool_approval`'s
    decision tree so graph + agent parks behave identically when the
    operator approves / rejects / lets the park time out / cancels.
    """
    if isinstance(payload, YieldTimeout):
        return "rejected", "timed-out"
    if isinstance(payload, YieldCancelled):
        return "rejected", payload.reason or "cancelled"
    if isinstance(payload, dict):
        raw = payload.get("decision")
        reason = payload.get("reason")
        if raw == "approved":
            return "approved", reason
        if raw == "rejected":
            return "rejected", reason
        return "rejected", "malformed approval payload (missing decision)"
    return "rejected", "malformed approval payload (non-dict)"


async def resume_graph_from_checkpoint(
    *,
    executor: "WorkspaceGraphExecutor",
    checkpoint: dict[str, Any],
    payload: "dict[str, Any] | YieldTimeout | YieldCancelled | Any",
) -> str:
    """Drive a graph executor's resume stream to completion.

    Parameters
    ----------
    executor
        Freshly-built :class:`WorkspaceGraphExecutor` for the parked
        session — same graph, same per-node resolvers, same state repo.
        The caller is responsible for wiring it the way the original
        invoke path did so the resumed superstep loop hits the same
        nodes / state.
    checkpoint
        The JSON-able snapshot stored in
        :attr:`ParkedState.graph_checkpoint`. Round-trips through
        :meth:`Graph.snapshot_state` / :meth:`Graph.restore_state`.
    payload
        Classified resume payload from
        :func:`primer.worker.yield_runtime.classify_resume_payload`.
        Approved decisions let the executor's bypassed dispatch run as
        normal; rejected / timeout / cancelled decisions monkeypatch
        ``_dispatch_toolcall_with_bypass`` to raise
        :class:`_ToolApprovalRejected` so the resume drain emits the
        ``tool_execution_failed`` terminal event per spec §4.8.

    Returns
    -------
    str
        ``"approved"`` if the operator approved (graph drained
        normally); ``"rejected"`` otherwise. Caller uses this to mark
        the session ENDED — graphs that complete via this path never
        re-enqueue, mirroring :meth:`_GraphTurnDriver.last_done_reason`
        (always ``"graph_ended"``).
    """
    # Local import to keep this module's import surface tiny — the
    # worker pool imports it lazily inside _handle_resume.
    from primer.graph.base import _ToolApprovalRejected

    decision, reason = _decision_from_payload(payload)

    if decision != "approved":
        # Override the executor's bypass dispatch so every pending
        # ToolCall surfaces as a rejection. ``resume_from_checkpoint``
        # catches ``_ToolApprovalRejected`` and stamps each pending
        # node with ``ended_detail='tool_execution_failed'``.
        rejection_reason = reason or "rejected"

        async def _rejecting_dispatch(node, arguments):  # type: ignore[no-untyped-def]
            raise _ToolApprovalRejected(rejection_reason)

        executor._dispatch_toolcall_with_bypass = _rejecting_dispatch  # type: ignore[assignment]

    # Drain the resume stream. Events are intentionally discarded here
    # — streaming-tap subscribers attached via the executor's own
    # fan-out still receive them. Mirrors :class:`_GraphTurnDriver.invoke`.
    async for _ev in executor.resume_from_checkpoint(checkpoint):
        pass

    return decision


__all__ = ["resume_graph_from_checkpoint"]
