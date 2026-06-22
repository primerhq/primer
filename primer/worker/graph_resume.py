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
# Re-exported for back-compat; the canonical classifier lives in
# yield_runtime so the graph + agent resume paths cannot drift.
from primer.worker.yield_runtime import (
    classify_approval_payload as _decision_from_payload,
)


if TYPE_CHECKING:
    from primer.graph.workspace_executor import WorkspaceGraphExecutor


logger = logging.getLogger(__name__)


async def resume_graph_from_checkpoint(
    *,
    executor: "WorkspaceGraphExecutor",
    checkpoint: dict[str, Any],
    payload: "dict[str, Any] | YieldTimeout | YieldCancelled | Any",
    resumed_tcid: str | None = None,
    agent_tool_result: "Any | None" = None,
) -> "tuple[str, Any | None]":
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

    ``resumed_tcid`` (multi-event park) selects which pending node the
    human replied to; ``agent_tool_result`` is the tool-result Message for
    a resumed agent-node yield (ask_user answer). When the resume leaves
    other human-interaction nodes pending the executor re-raises
    :class:`YieldToWorker`; this function catches it and returns it so the
    worker can re-park on the remaining keys.

    Returns
    -------
    tuple[str, YieldToWorker | None]
        ``(decision, repark)``. ``decision`` is ``"approved"`` /
        ``"rejected"``. ``repark`` is the re-park ``YieldToWorker`` when
        nodes remain pending, else ``None`` (graph drained to completion).
    """
    # Local import to keep this module's import surface tiny — the
    # worker pool imports it lazily inside _handle_resume.
    from primer.graph._node_refs import _is_value_yield_toolcall
    from primer.graph.base import _ToolApprovalRejected, _PendingToolCall
    from primer.model.yield_ import YieldToWorker

    decision, reason = _decision_from_payload(payload)

    # A value-yielding tool_call node (e.g. ``system__ask_user``) does NOT
    # gate on an approve/reject decision: its node result IS the operator's
    # reply, computed by the executor from ``toolcall_payload`` via the tool's
    # resume hook. Detect that pending entry so we (a) hand the raw payload to
    # the executor and (b) skip the rejection bypass override below (which only
    # applies to approval gates).
    value_yield_toolcall = any(
        isinstance(p, _PendingToolCall)
        and p.tool_call_id == resumed_tcid
        and _is_value_yield_toolcall(p)
        for p in _pending_toolcalls_from(checkpoint)
    ) if resumed_tcid is not None else False

    # Only the tool_call-approval rejection path uses the bypass override; an
    # agent-node yield carries its result via ``agent_tool_result``, and a
    # value-yielding tool_call carries it via ``toolcall_payload``.
    if (
        decision != "approved"
        and agent_tool_result is None
        and not value_yield_toolcall
    ):
        rejection_reason = reason or "rejected"

        async def _rejecting_dispatch(node, arguments):  # type: ignore[no-untyped-def]
            raise _ToolApprovalRejected(rejection_reason)

        executor._dispatch_toolcall_with_bypass = _rejecting_dispatch  # type: ignore[assignment]

    repark: YieldToWorker | None = None
    try:
        async for _ev in executor.resume_from_checkpoint(
            checkpoint,
            resumed_tcid=resumed_tcid,
            agent_tool_result=agent_tool_result,
            toolcall_payload=payload if value_yield_toolcall else None,
        ):
            pass
    except YieldToWorker as yld:
        repark = yld

    return decision, repark


def _pending_toolcalls_from(checkpoint: dict[str, Any]) -> "list[Any]":
    """Reconstruct the checkpoint's ``_PendingToolCall`` entries.

    Used only to classify whether the resumed entry is a value-yielding
    tool_call (ask_user) before we restore the executor's full state; reads
    the same ``pending_toolcalls`` list :meth:`Graph.restore_state` consumes.
    """
    from primer.graph.base import _PendingToolCall

    return [
        _PendingToolCall(
            node_id=raw["node_id"],
            tool_call_id=raw["tool_call_id"],
            parked_event_key=raw["parked_event_key"],
            arguments=dict(raw.get("arguments") or {}),
            tool_name=raw.get("tool_name"),
            resume_metadata=dict(raw.get("resume_metadata") or {}),
        )
        for raw in (checkpoint.get("pending_toolcalls") or [])
    ]


__all__ = ["resume_graph_from_checkpoint"]
