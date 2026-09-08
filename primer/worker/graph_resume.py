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
    from primer.worker.pool import WorkerPool


logger = logging.getLogger(__name__)


async def resume_graph_from_checkpoint(
    *,
    executor: "WorkspaceGraphExecutor",
    checkpoint: dict[str, Any],
    payload: "dict[str, Any] | YieldTimeout | YieldCancelled | Any",
    resumed_tcid: str | None = None,
    agent_tool_result: "Any | None" = None,
    pool: "WorkerPool | None" = None,
    session: "Any | None" = None,
    node_tool_call_seq: dict[str, int] | None = None,
    resolved_tool_wait: "dict[str, list] | None" = None,
) -> "tuple[str, Any | None, dict[str, int]]":
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
    pool, session
        01a0690a piece 3/3 (Gap 2). When both are given, every event the
        resumed drain yields is tapped through the SAME persistence
        vocabulary the live turn uses (translate_stream_event ->
        WorkspaceMessageWriter, see graph/base.py's own "flows through
        translate_stream_event -> WorkspaceMessageWriter.append" comment)
        instead of being silently discarded — a graph that keeps running
        past the resumed node (the node's own LLM continuing, or
        downstream nodes) previously left everything it did, post-resume,
        missing from the durable record. ``None`` (the default) skips
        tapping entirely — existing direct callers that only care about
        the drain's control flow, not its persistence, are unaffected.
    node_tool_call_seq
        Per-node scoped-id mint-seq high-water mark from
        :attr:`ParkedState.node_tool_call_seq` (piece 1) — seeds the tap's
        fresh ``_CoalesceState`` so it continues minting past whatever
        this turn_no already used pre-park, instead of restarting every
        node's counter at 0 and re-minting colliding ids (turn_no does
        not bump until on_release, so the resume drain observes the
        SAME turn_no the pre-park turn did). ``None`` starts empty (a
        checkpoint written before piece 1, or a park with no prior mints).

    ``resumed_tcid`` (multi-event park) selects which pending node the
    human replied to; ``agent_tool_result`` is the tool-result Message for
    a resumed agent-node yield (ask_user answer). ``resolved_tool_wait``
    (01a0518b boundary d, graph third-list) is the tool_wait-batch
    sibling of those two: a ``node_id -> ToolResultPart list`` map for
    every co-pending tool_wait batch the CALLER has already determined is
    fully terminal, threaded straight through to
    :meth:`WorkspaceGraphExecutor.resume_from_checkpoint`'s own
    identically-named parameter. When the resume leaves other
    human-interaction nodes OR tool_wait batches pending, the executor
    re-raises :class:`YieldToWorker` (a co-pending human gate) or
    :class:`ToolWaitPark` (tool_wait-only remainder); this function
    catches EITHER and returns it so the worker can re-park on the
    remaining keys.

    Returns
    -------
    tuple[str, YieldToWorker | ToolWaitPark | None, dict[str, int]]
        ``(decision, repark, node_tool_call_seq)``. ``decision`` is
        ``"approved"`` / ``"rejected"``. ``repark`` is the re-park
        exception when nodes/batches remain pending, else ``None`` (graph
        drained to completion). ``node_tool_call_seq`` is the tap's
        (possibly advanced, if pool/session were given) per-node mint-seq
        snapshot — the caller threads it into the repark's own
        ``ParkedState.node_tool_call_seq`` so a park -> resume -> repark ->
        resume chain never resets ({} when tapping was skipped).
    """
    # Local import to keep this module's import surface tiny — the
    # worker pool imports it lazily inside _handle_resume.
    from primer.graph._node_refs import _is_value_yield_toolcall
    from primer.graph.base import _ToolApprovalRejected, _PendingToolCall
    from primer.model.yield_ import ToolWaitPark, YieldToWorker

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

    tap = None
    if pool is not None and session is not None:
        tap = await _ResumeDrainTap.create(
            pool, session, node_tool_call_seq=node_tool_call_seq,
            tool_calls_as_claims_enabled=getattr(
                executor, "_tool_calls_as_claims_enabled", False,
            ),
        )
        # 7a gate review (verdict item 3): bind the SAME coalesce_state
        # the tap already populates (via translate_stream_event, as it
        # observes every event this drain yields) onto the executor
        # itself. Without this, resolve_scoped_call is always None on
        # every resume - executor_builders.build_graph_executor never
        # binds it, only dispatch.py's own live-turn loop does - so a
        # resumed node's own continuation could never route a SECOND
        # claims batch through the tool-dispatch seam at all, regardless
        # of the flag: run_agent_turn's routing gate requires BOTH the
        # flag AND a non-None resolver. Event ordering guarantees the
        # tap's own translate_stream_event call for a ToolCallStart/End
        # pair always runs (stashing into this SAME coalesce_state)
        # before _dispatch_as_claims would ever call resolve_scoped_call
        # for it - the LLM's own tool-call events stream out of
        # run_agent_turn before its internal dispatch loop acts on them.
        executor.bind_coalesce_state(tap.coalesce_state)

    repark: "YieldToWorker | ToolWaitPark | None" = None
    try:
        async for ev in executor.resume_from_checkpoint(
            checkpoint,
            resumed_tcid=resumed_tcid,
            agent_tool_result=agent_tool_result,
            toolcall_payload=payload if value_yield_toolcall else None,
            resolved_tool_wait=resolved_tool_wait,
        ):
            if tap is not None:
                await tap.observe(ev)
    except (YieldToWorker, ToolWaitPark) as repark_exc:
        # 01a0518b boundary d: a co-pending human gate always wins the
        # executor's own re-park choice (YieldToWorker); a tool_wait-only
        # remainder re-parks as ToolWaitPark instead (see
        # _build_pending_park_exception) - both carry a dynamically-set
        # ``graph_checkpoint`` (YieldToWorker's own long-standing
        # convention, extended to ToolWaitPark for this same purpose), so
        # one arm handles either shape identically from here on.
        repark = repark_exc
        if tap is not None and repark.graph_checkpoint is not None:
            # 01a0690a: the SAME stash the original park uses
            # (dispatch.py's catch) -- this repark never goes through
            # dispatch.py, so any NEW pending entry this drain just
            # produced gets its scoped id stashed here instead.
            from primer.session.persistence import stash_graph_scoped_ids
            stash_graph_scoped_ids(repark.graph_checkpoint, tap.coalesce_state)

    out_node_tool_call_seq = (
        dict(tap.coalesce_state.tool_call_seq) if tap is not None else {}
    )
    if tap is not None:
        await tap.finish()

    return decision, repark, out_node_tool_call_seq


class _ResumeDrainTap:
    """Persists a graph resume drain's StreamEvents via the live path's own
    persistence vocabulary (01a0690a piece 3/3 — Gap 2).

    Best-effort throughout: any setup or per-event failure is logged and
    the tap disables itself for the rest of the drain rather than raising
    — a persistence hiccup must never fail an otherwise-successful resume
    (same doctrine as ``persist_resume_tool_result_record_for_graph``,
    piece 2).
    """

    def __init__(
        self, *, pool, session, writer, coalesce_state, turn_no,
        tool_calls_as_claims_enabled: bool = False,
    ) -> None:
        self._pool = pool
        self._session = session
        self._writer = writer
        self.coalesce_state = coalesce_state
        self._turn_no = turn_no
        # 7a gate review (verdict item 3): threaded explicitly rather
        # than read off the executor here - this class has no reference
        # to it, and the caller (resume_graph_from_checkpoint) already
        # has the executor in scope right where the tap is built.
        self._tool_calls_as_claims_enabled = tool_calls_as_claims_enabled

    @classmethod
    async def create(
        cls, pool: "WorkerPool", session, *,
        node_tool_call_seq: dict[str, int] | None,
        tool_calls_as_claims_enabled: bool = False,
    ) -> "_ResumeDrainTap":
        from primer.session.persistence import _CoalesceState, WorkspaceMessageWriter

        state = _CoalesceState()
        # Seed past whatever this turn_no already minted pre-park (see this
        # module's docstring on node_tool_call_seq) instead of restarting
        # every node's counter at 0 and re-minting colliding ids.
        state.tool_call_seq = dict(node_tool_call_seq or {})
        writer = None
        try:
            # Fresh read rather than trusting the session object threaded
            # down the call chain: piece 2's write (resume_graph_engine
            # calls it immediately before this) may already have bumped
            # last_seq in storage, and starting this writer from a stale
            # count would collide with piece 2's just-written seq.
            from primer.model.workspace_session import WorkspaceSession

            last_seq = session.last_seq
            if pool._storage is not None:
                fresh = await pool._storage.get_storage(WorkspaceSession).get(
                    session.id,
                )
                if fresh is not None:
                    last_seq = fresh.last_seq
            ws = await pool._load_workspace_for_persist(session.workspace_id)
            writer = WorkspaceMessageWriter(
                workspace_io=ws, session_id=session.id, start_seq=last_seq,
            )
        except Exception:  # noqa: BLE001 - best-effort, see class docstring
            logger.exception(
                "resume: failed to set up graph resume-drain tap for "
                "session %s",
                session.id,
            )
        return cls(
            pool=pool, session=session, writer=writer, coalesce_state=state,
            turn_no=session.turn_no,
            tool_calls_as_claims_enabled=tool_calls_as_claims_enabled,
        )

    async def observe(self, event: Any) -> None:
        if self._writer is None:
            return
        from primer.session.persistence import (
            stash_and_flush_tool_call_record,
            translate_stream_event,
        )

        try:
            result = translate_stream_event(
                event, self.coalesce_state, turn_no=self._turn_no,
            )
            if result is None:
                return
            records = result if isinstance(result, list) else [result]
            for rec in records:
                seq = await self._writer.append(rec)
                # 7a gate review (verdict item 3): SAME shared helper
                # dispatch.py's own live-turn loop calls - a resumed
                # node's continuation dispatching a SECOND claims batch
                # needs this drain's own tap to stash+flush its TOOL_CALL
                # records exactly like the live path does, or
                # resolve_scoped_call's "durable record" proof would be
                # false and the eventual row-creation would raise loudly
                # instead of the seam actually working. No-op when the
                # flag is off (item A's own gating) - byte-identical to
                # before this fix for the classic approval-gate resume
                # path, which also uses this tap.
                await stash_and_flush_tool_call_record(
                    rec, seq, coalesce_state=self.coalesce_state,
                    writer=self._writer,
                    tool_calls_as_claims_enabled=self._tool_calls_as_claims_enabled,
                )
                if self._pool._event_bus is not None:
                    await self._pool._event_bus.publish(
                        f"session:{self._session.id}:tick", {"seq": seq},
                    )
        except Exception:  # noqa: BLE001 - best-effort, see class docstring
            logger.exception(
                "resume: graph resume-drain tap failed for session %s -"
                " disabling for the rest of this drain",
                self._session.id,
            )
            self._writer = None

    async def finish(self) -> None:
        if self._writer is None:
            return
        try:
            from primer.model.workspace_session import WorkspaceSession

            await self._writer.flush()
            if self._pool._storage is not None:
                storage = self._pool._storage.get_storage(WorkspaceSession)
                await storage.update(self._session.model_copy(
                    update={"last_seq": self._writer.last_seq},
                ))
        except Exception:  # noqa: BLE001 - best-effort, see class docstring
            logger.exception(
                "resume: failed to flush graph resume-drain tap for "
                "session %s",
                self._session.id,
            )


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
