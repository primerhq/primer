"""Graph-session resume / repark coordinator for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). The
graph-resume cluster drives a graph-bound session parked at a human-interaction
node (ToolCall approval / ask_user / nested invoke_agent yield) back to
completion or re-parks it on the remaining keys.

Each function takes the :class:`~primer.worker.pool.WorkerPool` instance as
``pool`` and reads / calls the same bound deps and sibling methods the original
``WorkerPool`` methods did (``pool._storage``, ``pool._end_session``,
``pool._build_graph_executor``, ``pool._build_invocation_services``, ...). The
pool keeps thin delegating methods so call sites and test monkeypatches still
resolve through the instance: when one routine calls another (e.g.
``pool._graph_agent_tool_result``) it dispatches through the patchable instance
method.

Lazy imports inside each function preserve the original module's tiny import
surface (the worker pool imported these dependencies inside the methods).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from primer.worker.yield_resume_registry import get_resume_hook
from primer.worker.yield_runtime import (
    classify_approval_payload,
    classify_resume_payload,
    ParkedState,
)

if TYPE_CHECKING:
    from primer.worker.pool import WorkerPool

logger = logging.getLogger(__name__)


async def write_approval_record_for_graph(
    pool: "WorkerPool", *, session, checkpoint: dict, tcid, payload,
) -> None:
    """Persist the resolved approval decision for a graph tool-call gate.

    Resolves the SPECIFIC pending entry ``tcid`` names via
    :func:`primer.session.pending_gates.resolve_pending_gate` -- the same
    shared helper the REST respond-time write (``tool_approval.py``'s
    ``_publish_decision``) uses -- so the resume-time and respond-time
    writes for the same gate can never disagree about which entry's
    metadata a decision describes, and both now carry the gate's real
    ``event_key`` into ``gate_event_key`` for idempotent dedup (01a068da).

    This also picks up a real bugfix: an approval gate raised by a
    ToolCall NODE (as opposed to an agent-node's own tool call) used to
    resolve through ``pending_dispatch``, a denormalised channel-prompt
    view that only ever carried ``original_call`` -- ``policy_id`` /
    ``approval_type`` / ``gate_reason`` / ``approvers`` were silently
    dropped from every such record. Resolving via ``pending_toolcalls``
    (the raw checkpoint field the shared helper reads) carries the full
    metadata tool_manager.py originally stamped.

    A tcid that resolves to nothing (not an approval gate, or the legacy
    single-event drain-all with no tcid) is skipped. Best-effort: a
    missing entry or write failure is logged + swallowed
    (``write_approval_record``'s own contract).

    01a07be5 gate-review-2 finding 2: always passes
    ``warn_on_decision_mismatch=True``, not just for a synthesised
    timeout/cancel. The flip-winner (whichever publish actually advanced
    this park) and the record-winner (whichever write_approval_record
    call wins the gate_event_key race) are INDEPENDENT races -- a
    respond-time writer can lose the flip but still win the record race
    with a DIFFERENT decision than the one that actually resumed the
    park. This resume-time write is the one place that knows the TRUE
    resumed decision (``payload``, whatever actually flipped the row),
    so it is always worth checking a losing ConflictError against: an
    agreeing race (the overwhelming common case, one operator, one
    decision) stays quiet at DEBUG either way.
    """
    from primer.agent.approval_record import (
        record_from_parked_blob,
        write_approval_record,
    )
    from primer.model.tool_approval import ToolApprovalRecord
    from primer.session.pending_gates import resolve_pending_gate

    if not tcid:
        return
    gate = resolve_pending_gate(
        {"graph_checkpoint": checkpoint}, tool_call_id=tcid, kind="_approval",
    )
    if gate is None:
        return
    decision, reason = classify_approval_payload(payload)
    blob = {
        "tool_call_id": tcid,
        "yielded": {"resume_metadata": gate.get("resume_metadata") or {}},
    }
    record = record_from_parked_blob(
        blob=blob,
        decision=decision,
        reason=reason,
        agent_id=getattr(session.binding, "agent_id", None),
        session_id=session.id,
        requested_at=session.parked_at,
        # Stamped by the respond route (P6); synthesized verdicts
        # (timeout/cancel) carry none.
        decided_by=(
            payload.get("decided_by") if isinstance(payload, dict) else None
        ),
        gate_event_key=gate.get("event_key"),
    )
    storage = (
        pool._storage.get_storage(ToolApprovalRecord)
        if pool._storage is not None
        else None
    )
    await write_approval_record(
        storage, record, warn_on_decision_mismatch=True,
    )


async def resume_graph_engine(pool: "WorkerPool", session, parked):
    """Resume a graph-bound session parked at a ToolCall approval.

    Adapted from the (dead) _handle_graph_resume: always terminal (graph
    sessions run to completion in one resume), so this returns a drop-lease
    outcome with ENDED status written to the row.

    01a0518b boundary d (graph third-list): a tool_wait batch can be
    co-pending alongside the human gate this function exists to resume -
    its own reply carries NO information about tool_wait readiness (an
    approval/ask_user reply's tcid never matches a tool_wait batch's own
    node-qualified wake key), so this independently re-checks EVERY
    co-pending batch via :func:`resolve_ready_graph_tool_waits` on each
    drain cycle rather than assuming none are ready. Covers "tools finish
    before the gate is answered": the human gate is still what routes the
    resume here (a tool_wait-only wake for a PURE park routes through
    :func:`primer.worker.tool_wait_resume_coordinator.resume_graph_tool_wait`
    instead - see that module's own kind-based dispatch), but a co-pending
    batch that ALSO went terminal in the meantime must not be left
    stranded until some LATER unrelated reply happens to drain it.
    """
    from primer.model.tool_call_task import ToolCallTask
    from primer.worker.graph_resume import resume_graph_from_checkpoint
    from primer.worker.tool_wait_resume_coordinator import (
        persist_resume_tool_result_records,
        resolve_ready_graph_tool_waits,
    )

    sid = session.id
    assert parked.graph_checkpoint is not None
    task_storage = (
        pool._storage.get_storage(ToolCallTask)
        if pool._storage is not None else None
    )

    if session.parked_at is None:
        logger.error(
            "resume: graph session %s resumable but parked_at=None -"
            " ending failed", sid,
        )
        return await pool._end_session(session, reason="failed")

    resume_payload = classify_resume_payload(parked, parked_at=session.parked_at)
    workspace = await pool._load_workspace_for_persist(session.workspace_id)
    try:
        executor_or_driver = await pool._build_graph_executor(session, workspace)
    except Exception:
        logger.exception(
            "resume: failed to build graph executor for session %s -"
            " ending failed", sid,
        )
        return await pool._end_session(session, reason="failed")
    executor = getattr(executor_or_driver, "_executor", executor_or_driver)

    # Replies to drain this cycle. A multi-event park accumulates every
    # reply that arrived into ``resume_event_payloads`` (dispatch_key ->
    # reply, see primer.session.yields._dispatch_key_for - 01a0518f: the
    # dict key is node-qualified for a graph park so two fan-out siblings
    # sharing a raw tool_call_id don't overwrite each other's reply); we
    # drain them ALL so a concurrent second reply isn't lost. Every entry
    # still carries its own full ``event_key`` verbatim regardless of the
    # dict key's shape, so the bare tool_call_id downstream matching
    # (graph_value_yield_toolcall etc., which key checkpoint entries by
    # tool_call_id, not the compound dispatch key) is recovered from
    # THAT, the same rsplit-last-segment extraction used everywhere else
    # on this path - never from the dict key itself. A single-event park
    # / timeout / cancel uses the singular path (classified payload,
    # resumed_tcid from the fired key, or None for the legacy drain-all).
    raw_state = session.parked_state or {}
    payloads_map = raw_state.get("resume_event_payloads")
    ck = parked.graph_checkpoint
    if payloads_map:
        replies = [
            (
                (entry or {}).get("event_key", "").rsplit(":", 1)[-1] or None,
                (entry or {}).get("payload") or {},
            )
            for entry in payloads_map.values()
        ]
    else:
        resume_event_key = raw_state.get("resume_event_key")
        resumed_tcid = (
            resume_event_key.rsplit(":", 1)[-1] if resume_event_key else None
        )
        replies = [(resumed_tcid, resume_payload.payload)]

    repark = None
    # 01a0690a piece 3: per-node mint-seq high-water mark, seeded from the
    # original park and advanced after each drain below -- threaded into
    # any repark this loop produces so a chain of resumes never re-mints a
    # colliding scoped id.
    node_tool_call_seq = dict(getattr(parked, "node_tool_call_seq", None) or {})
    for tcid, payload in replies:
        # Unified nested-yield: when the parked agent-node yielded from
        # INSIDE a nested invoke_agent invocation, its pending entry carries
        # a continuation ``frames`` stack. Run the continuation walk to
        # unwind the subagent chain into a single tool_result FIRST; deliver
        # that as the node's agent_tool_result (Deliver), or re-park the
        # graph session on the deeper new leaf if a frame re-yielded
        # (Repark). The no-nested-frames path below is UNCHANGED.
        nested = pool._graph_nested_agent_yield(ck, tcid)
        if nested is not None:
            cont = await pool._resume_graph_continuation(
                session, parked, ck, nested, payload, workspace, executor,
            )
            # 01a07be5 gate-review-2 finding 1: the leaf this nested
            # entry unwinds can itself be an approval gate. write_
            # approval_record_for_graph already resolves via checkpoint
            # + tcid regardless of nesting -- pending_agent_yields'
            # top-level tool_name/event_key/resume_metadata are always
            # the LEAF's own values (frames/leaf are additive bookkeeping
            # for the continuation walk, not a different identity), and
            # the function no-ops internally for a non-"_approval" kind
            # -- so this call is safe unconditionally. Before this fix,
            # taking this branch skipped the write entirely: real
            # decisions AND terminal synthesis for a nested approval gate
            # left no audit record at all, silently. Written BEFORE the
            # repark_outcome check: tcid's own decision is settled by
            # payload regardless of whether the chain reparks deeper
            # afterward on a DIFFERENT, unrelated gate.
            await pool._write_approval_record_for_graph(
                session=session, checkpoint=ck, tcid=tcid, payload=payload,
            )
            if cont.repark_outcome is not None:
                return cont.repark_outcome
            agent_tool_result = cont.agent_tool_result
        else:
            agent_tool_result = await pool._graph_agent_tool_result(
                ck, tcid, payload,
            )
            # An approval gate is a pending tool-call yield (NOT an ask_user
            # agent yield, which carries agent_tool_result). Persist the
            # resolved decision for that gate exactly once per reply. A
            # value-yielding tool_call (ask_user) is NOT an approval gate:
            # its result is the operator's reply, fed back by the executor,
            # so skip the approval record for it.
            if agent_tool_result is None and not pool._graph_value_yield_toolcall(
                ck, tcid,
            ):
                await pool._write_approval_record_for_graph(
                    session=session, checkpoint=ck, tcid=tcid, payload=payload,
                )
        # 01a0690a piece 2: agent_tool_result is a synthesized delivery
        # (ask_user answer / unwound nested continuation) with no
        # StreamEvent of its own -- the only way it gets a durable display
        # record is an explicit write here.
        if agent_tool_result is not None:
            await pool._persist_resume_tool_result_record_for_graph(
                session=session, checkpoint=ck, tcid=tcid,
                agent_tool_result=agent_tool_result,
            )
        resolved_tool_wait: dict = {}
        resolved_tasks: dict = {}
        if task_storage is not None:
            resolved_tool_wait, resolved_tasks = await resolve_ready_graph_tool_waits(
                task_storage, ck.get("pending_tool_waits") or [],
            )
        try:
            _decision, repark, node_tool_call_seq = await resume_graph_from_checkpoint(
                executor=executor,
                checkpoint=ck,
                payload=payload,
                resumed_tcid=tcid,
                agent_tool_result=agent_tool_result,
                pool=pool,
                session=session,
                # 01a0690a piece 3: seed from wherever the LAST drain left
                # off, not the original park -- a multi-event park's later
                # replies resume through this same loop, and each drain's
                # own mints must not collide with the ones before it.
                node_tool_call_seq=node_tool_call_seq,
                resolved_tool_wait=resolved_tool_wait,
            )
        except Exception:
            logger.exception(
                "resume: graph executor for session %s raised during"
                " resume drain - ending failed", sid,
            )
            return await pool._end_session(session, reason="failed")
        if resolved_tasks:
            node_id_by_task_id = {
                task.id: node_id
                for node_id, tasks in resolved_tasks.items() for task in tasks
            }
            all_resolved_tasks = [
                t for tasks in resolved_tasks.values() for t in tasks
            ]
            await persist_resume_tool_result_records(
                pool, session, all_resolved_tasks,
                node_id_by_task_id=node_id_by_task_id,
            )
        if repark is None:
            break  # graph drained to completion
        ck = repark.graph_checkpoint  # resume the next reply from here

    if repark is not None:
        # Human-interaction nodes still pending (not yet replied to) ->
        # re-park on the remaining keys (no re-dispatch).
        return pool._repark_graph_outcome(
            session, repark, node_tool_call_seq=node_tool_call_seq,
        )

    # Drained to completion (the graph's own state.json carries the
    # real ended_reason; the session row mirrors _GraphTurnDriver).
    return await pool._end_session(session, reason="completed")


def graph_value_yield_toolcall(pool: "WorkerPool", checkpoint, tcid) -> bool:
    """True when ``tcid`` is a pending tool_call node that suspended on a
    value-yielding tool (e.g. ``ask_user``) rather than an approval gate.

    Such a node's result is the operator's reply (fed back by the executor
    via the tool's resume hook), so the worker must NOT write an approval
    record for it nor classify the reply as an approve/reject decision.
    """
    from primer.graph._node_refs import _PendingToolCall, _is_value_yield_toolcall

    matches = [
        e for e in (checkpoint.get("pending_toolcalls") or [])
        if e.get("tool_call_id") == tcid
    ]
    if len(matches) > 1:
        # 01a0518f: see write_approval_record_for_graph's comment above.
        logger.warning(
            "graph_value_yield_toolcall: %d pending_toolcalls share "
            "tool_call_id=%r; resolving the first",
            len(matches), tcid,
        )
    raw = matches[0] if matches else None
    if raw is None:
        return False
    entry = _PendingToolCall(
        node_id=raw["node_id"],
        tool_call_id=raw["tool_call_id"],
        parked_event_key=raw["parked_event_key"],
        arguments=dict(raw.get("arguments") or {}),
        tool_name=raw.get("tool_name"),
        resume_metadata=dict(raw.get("resume_metadata") or {}),
    )
    return _is_value_yield_toolcall(entry)


def graph_nested_agent_yield(pool: "WorkerPool", checkpoint, tcid):
    """Return the parked agent-node entry for ``tcid`` IFF it carries a
    nested continuation ``frames`` stack, else ``None``.

    A non-empty ``frames`` marks a node that yielded from inside a nested
    ``system__invoke_agent`` invocation; those resume through the
    continuation walk (:meth:`_resume_graph_continuation`) rather than the
    flat ask_user / approval path.
    """
    matches = [
        e for e in (checkpoint.get("pending_agent_yields") or [])
        if e.get("tool_call_id") == tcid
    ]
    if len(matches) > 1:
        # 01a0518f: see write_approval_record_for_graph's comment above.
        logger.warning(
            "graph_nested_agent_yield: %d pending_agent_yields share "
            "tool_call_id=%r; resolving the first",
            len(matches), tcid,
        )
    ay = matches[0] if matches else None
    if ay is None or not ay.get("frames"):
        return None
    return ay


async def resume_graph_continuation(
    pool: "WorkerPool", session, parked, checkpoint, ay, payload, workspace, executor,
):
    """Run the continuation walk for a graph-node's nested invoke_agent yield.

    ``ay`` is the checkpoint's pending_agent_yield entry (with ``frames`` +
    ``leaf``). Builds :class:`InvocationServices`, drives
    :func:`resume_continuation` over the subagent chain, and returns a tiny
    result carrying EITHER:

    * ``agent_tool_result`` - a ``role="tool"`` Message wrapping the unwound
      subagent result (keyed by the node's invoke_agent call id), to deliver
      into the parked graph node as its ``agent_tool_result`` (Deliver), or
    * ``repark_outcome`` - a ReleaseOutcome re-parking the GRAPH SESSION on
      the deeper new leaf when a frame re-yielded (Repark). The graph itself
      did NOT advance; only the nested subagent state changed.
    """
    from dataclasses import dataclass
    from primer.model.chat import Message
    from primer.worker.continuation import Repark, resume_continuation
    from primer.worker.frames import frames_from_jsonable
    from primer.model.yield_ import Yielded

    @dataclass
    class _ContResult:
        agent_tool_result: "Message | None" = None
        repark_outcome: "Any | None" = None

    # Graph-session continuation: the subagent callables only need worker
    # deps (storage / registry / approval), NOT an agent tool_manager - so
    # bind the services with tool_manager=None (the GraphFrame callables,
    # unused for a pure subagent yield, fail loudly if ever reached).
    services = pool._build_invocation_services(
        session, workspace, executor, None,
    )
    frames = frames_from_jsonable(list(ay.get("frames") or []))
    leaf = Yielded.from_jsonable(ay["leaf"])
    outcome = await resume_continuation(frames, leaf, payload, services)
    if isinstance(outcome, Repark):
        return _ContResult(
            repark_outcome=pool._repark_graph_continuation(
                session, parked, checkpoint, ay, outcome,
            ),
        )
    # Deliver: the tool_result is keyed by the node's invoke_agent call id
    # (the outermost AgentFrame's tool_call_id), which pairs with the
    # invoke_agent tool_use in the node's rehydrated history.
    return _ContResult(
        agent_tool_result=Message(role="tool", parts=[outcome.tool_result]),
    )


def repark_graph_continuation(pool: "WorkerPool", session, parked, checkpoint, ay, outcome):
    """Re-park a GRAPH SESSION whose node's nested subagent re-yielded.

    The graph did not advance: only the nested subagent chain changed.
    Persist the SAME ``graph_checkpoint`` with this node's pending entry's
    ``frames`` / ``leaf`` replaced by the reconstructed stack + new deeper
    leaf, and park on the new leaf's event key. Mirrors
    :meth:`_repark_graph_outcome` for the ParkRequest / timeout shape.
    """
    from copy import deepcopy
    from datetime import timedelta
    from primer.int.claim import ParkRequest, ReleaseOutcome
    from primer.worker.frames import frames_to_jsonable

    leaf = outcome.leaf
    new_ck = deepcopy(checkpoint)
    for e in new_ck.get("pending_agent_yields") or []:
        if e.get("tool_call_id") == ay.get("tool_call_id"):
            e["frames"] = frames_to_jsonable(list(outcome.frames))
            e["leaf"] = leaf.to_jsonable()
            # The node still awaits the SAME invoke_agent call, but the
            # deeper leaf's event/metadata moved - re-point the entry's
            # await key so the park + drain selection track the new leaf.
            e["event_key"] = leaf.event_key
            e["tool_name"] = leaf.tool_name
            e["resume_metadata"] = dict(leaf.resume_metadata or {})
            break

    now = datetime.now(timezone.utc)
    timeout = leaf.timeout if leaf.timeout is not None else 3600.0
    parked_state = ParkedState(
        yielded=leaf,
        llm_messages=[],
        turn_no=session.turn_no,
        started_at=now,
        tool_call_id=parked.tool_call_id,
        graph_checkpoint=new_ck,
        # 01a0690a: the continuation walk mints no new tool-call events
        # (it operates purely on the frames/leaf stack) -- nothing to
        # seed with, so carry the original park's snapshot forward
        # unchanged, same doctrine as 0b4e8bfc's repark_continuation.
        node_tool_call_seq=getattr(parked, "node_tool_call_seq", None),
    )
    return ReleaseOutcome(
        success=True,
        drop_lease=True,
        park=ParkRequest(
            parked_state=parked_state.to_jsonable(),
            parked_event_key=leaf.event_key,
            parked_event_keys=getattr(leaf, "event_keys", None),
            parked_until=now + timedelta(seconds=timeout),
            parked_at=now,
        ),
    )


async def graph_agent_tool_result(pool: "WorkerPool", checkpoint, tcid, payload):
    """Build the tool_result Message an agent-node yield continues from
    (e.g. the ask_user answer). Returns None for tool_call approvals /
    agent-node approvals (those take the bypass/verdict path) or when
    the fired tcid is not a hook-backed agent yield."""
    from primer.model.chat import Message, ToolResultPart

    matches = [
        e for e in (checkpoint.get("pending_agent_yields") or [])
        if e.get("tool_call_id") == tcid
    ]
    if len(matches) > 1:
        # 01a0518f: see write_approval_record_for_graph's comment above.
        logger.warning(
            "graph_agent_tool_result: %d pending_agent_yields share "
            "tool_call_id=%r; resolving the first",
            len(matches), tcid,
        )
    ay = matches[0] if matches else None
    if ay is None or ay.get("tool_name") in (None, "_approval"):
        return None
    try:
        hook = get_resume_hook(ay["tool_name"])
        hook_result = hook(ay.get("resume_metadata") or {}, payload)
        if asyncio.iscoroutine(hook_result):
            hook_result = await hook_result
        return Message(role="tool", parts=[ToolResultPart(
            id=tcid or ay["tool_call_id"],
            output=hook_result.output, error=hook_result.is_error)])
    except Exception:
        logger.exception("resume: ask_user hook raised for tcid %s", tcid)
        return Message(role="tool", parts=[ToolResultPart(
            id=tcid or ay["tool_call_id"], output="resume failed",
            error=True)])


async def persist_resume_tool_result_record_for_graph(
    pool: "WorkerPool", *, session, checkpoint, tcid, agent_tool_result,
) -> None:
    """Write the modern TOOL_RESULT counterpart for a resumed graph yield.

    01a0690a piece 2/3. ``agent_tool_result`` (built by
    :func:`graph_agent_tool_result` or the nested-continuation walk) is a
    pure in-memory ``Message`` fed into the resumed node's LLM history for
    continuation -- it has no corresponding StreamEvent, so no amount of
    tapping the resume drain (piece 3) ever catches it. Mirrors
    ``session_resume_coordinator._persist_resume_tool_result_record``
    (0b4e8bfc / 01a068ea-dc95) for the agent path, node-tagged since graph
    records carry a ``node_id`` the agent path has none of. ``call_id`` uses
    the matched pending entry's ``scoped_tool_call_id`` (piece 1's stash),
    falling back to the raw ``tcid`` for a checkpoint written before that
    field existed.

    A value-yielding tool_call's resume (ask_user answered via a
    _PendingToolCall, not a _PendingAgentYield) has no durable record gap
    to begin with: the node re-runs for real through _dispatch_toolcall_
    with_bypass, so its ToolCallStart/End/Result flow through the live
    _GraphNodeEvent-tapped pipeline exactly like any other execution --
    that's why this only searches pending_agent_yields.

    Best-effort, same doctrine as the agent-path sibling: a write failure
    here must not fail an otherwise-successful resume.
    """
    if pool._storage is None or agent_tool_result is None:
        return
    from primer.model.chat import ToolResultPart
    from primer.model.workspace_session import (
        SessionMessageKind,
        SessionMessageRecord,
        WorkspaceSession,
    )
    from primer.session.persistence import WorkspaceMessageWriter

    tool_result_part = next(
        (p for p in agent_tool_result.parts if isinstance(p, ToolResultPart)),
        None,
    )
    if tool_result_part is None:
        return

    node_id = None
    scoped_call_id = None
    for e in (checkpoint.get("pending_agent_yields") or []):
        if e.get("tool_call_id") == tcid:
            node_id = e.get("node_id")
            scoped_call_id = e.get("scoped_tool_call_id")
            break

    try:
        ws = await pool._load_workspace_for_persist(session.workspace_id)
        writer = WorkspaceMessageWriter(
            workspace_io=ws, session_id=session.id, start_seq=session.last_seq,
        )
        new_seq = await writer.append(SessionMessageRecord(
            seq=1,  # overwritten by the writer's monotonic counter
            kind=SessionMessageKind.TOOL_RESULT,
            payload={
                "call_id": scoped_call_id or tcid or tool_result_part.id,
                "output": tool_result_part.output,
                "error": tool_result_part.error,
            },
            node_id=node_id,
            created_at=datetime.now(timezone.utc),
        ))
        await writer.flush()
        storage = pool._storage.get_storage(WorkspaceSession)
        await storage.update(session.model_copy(update={"last_seq": new_seq}))
        if pool._event_bus is not None:
            try:
                await pool._event_bus.publish(
                    f"session:{session.id}:tick", {"seq": new_seq},
                )
            except Exception:  # noqa: BLE001 - advisory
                logger.exception(
                    "resume: tick publish failed for graph session %s",
                    session.id,
                )
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        logger.exception(
            "resume: failed to persist modern TOOL_RESULT record for "
            "graph session %s",
            session.id,
        )


def repark_graph_outcome(
    pool: "WorkerPool", session, repark, *, node_tool_call_seq=None,
):
    """Build a ReleaseOutcome that re-parks a graph session on the
    remaining pending set after one reply / tool_wait batch was resumed.

    ``repark`` is whatever :func:`primer.worker.graph_resume.
    resume_graph_from_checkpoint` caught: a ``YieldToWorker`` (a
    co-pending human gate remains - the classic shape) or a
    ``ToolWaitPark`` (01a0518b boundary d - only tool_wait batches remain,
    no human gate). Dispatches on type since the two exceptions carry
    unrelated field shapes (``.yielded``/``.tool_call_id`` vs
    ``.outstanding_task_ids``/``.notifying_results``) - see each helper's
    own docstring.
    """
    from primer.model.yield_ import ToolWaitPark

    if isinstance(repark, ToolWaitPark):
        return _repark_graph_tool_wait_outcome(
            session, repark, node_tool_call_seq=node_tool_call_seq,
        )
    return _repark_graph_yield_outcome(
        session, repark, node_tool_call_seq=node_tool_call_seq,
    )


def _repark_graph_yield_outcome(session, repark, *, node_tool_call_seq=None):
    """Build a ReleaseOutcome that re-parks a graph session on the
    remaining human-interaction keys after one reply was resumed.

    ``node_tool_call_seq`` (01a0690a piece 3): the resume drain's own
    per-node mint-seq snapshot -- carried into the new park so a further
    resume of THIS repark seeds past whatever THIS drain just minted,
    instead of the stale pre-park snapshot the checkpoint started with.
    """
    from datetime import timedelta
    from primer.int.claim import ParkRequest, ReleaseOutcome

    now = datetime.now(timezone.utc)
    timeout = repark.yielded.timeout if repark.yielded.timeout is not None else 3600.0
    parked_state = ParkedState(
        yielded=repark.yielded,
        llm_messages=[],
        turn_no=session.turn_no,
        started_at=now,
        tool_call_id=repark.tool_call_id,
        graph_checkpoint=repark.graph_checkpoint,
        node_tool_call_seq=node_tool_call_seq or None,
    )
    return ReleaseOutcome(
        success=True,
        drop_lease=True,
        park=ParkRequest(
            parked_state=parked_state.to_jsonable(),
            parked_event_key=repark.yielded.event_key,
            parked_event_keys=repark.yielded.event_keys,
            parked_until=now + timedelta(seconds=timeout),
            parked_at=now,
        ),
    )


def _repark_graph_tool_wait_outcome(session, repark, *, node_tool_call_seq=None):
    """Build a ReleaseOutcome that re-parks a graph session on the
    remaining tool_wait batch(es) after a co-pending human gate resumed
    and left ``_pending_tool_waits`` non-empty (01a0518b boundary d).

    Pure re-write, no new ``ToolCallTask`` rows: every batch still in
    ``repark.graph_checkpoint['pending_tool_waits']`` was already
    materialized (rows + claim-engine upserts) at the ORIGINAL park time
    - the classic ``except YieldToWorker`` branch in dispatch.py runs
    ``_materialize_pending_tool_wait_rows`` for exactly this reason, even
    though a human gate was ALSO pending then. This just recomputes each
    batch's wake key (a pure function - see ``tool_wait_event_key``) and
    writes a fresh ``ParkRequest`` pointing at the same already-claimable
    rows. ``node_tool_call_seq`` is threaded through unchanged for the
    same reason ``_repark_graph_yield_outcome`` does - a FURTHER resume
    of this repark (a node's own next dispatch round) must not re-mint a
    colliding scoped id.
    """
    from datetime import timedelta
    from primer.int.claim import ParkRequest, ReleaseOutcome
    from primer.session.yields import tool_wait_event_key
    from primer.worker.yield_runtime import ToolWaitParkedState

    graph_checkpoint = repark.graph_checkpoint
    pending_tool_waits = list((graph_checkpoint or {}).get("pending_tool_waits") or [])
    wake_keys = [
        tool_wait_event_key(
            session.id, session.turn_no,
            scoped_task_id=(
                list(pw["outstanding_task_ids"])
                + [sid for sid, _ in pw["notifying_results"]]
            )[0],
        )
        for pw in pending_tool_waits
    ]
    now = datetime.now(timezone.utc)
    timeout = 3600.0
    parked_state = ToolWaitParkedState(
        outstanding_task_ids=list(repark.outstanding_task_ids),
        notifying_task_ids=[sid for sid, _ in repark.notifying_results],
        event_key=wake_keys[0] if wake_keys else repark.event_key,
        llm_messages=list(repark.llm_messages or []),
        turn_no=session.turn_no,
        started_at=now,
        graph_checkpoint=graph_checkpoint,
        node_tool_call_seq=node_tool_call_seq or None,
    )
    return ReleaseOutcome(
        success=True,
        drop_lease=True,
        park=ParkRequest(
            parked_state=parked_state.to_jsonable(),
            parked_event_key=wake_keys[0] if wake_keys else repark.event_key,
            parked_event_keys=wake_keys or None,
            parked_until=now + timedelta(seconds=timeout),
            parked_at=now,
        ),
    )
