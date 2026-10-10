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

from primer.worker.yield_resume_registry import ResumeContext, get_resume_hook
from primer.worker.yield_runtime import (
    classify_approval_payload,
    classify_marker_payload,
    classify_resume_payload,
    ParkedState,
)

if TYPE_CHECKING:
    from primer.worker.pool import WorkerPool

logger = logging.getLogger(__name__)


async def write_approval_record_for_graph(
    pool: "WorkerPool", *, session, checkpoint: dict, tcid, payload, event_key: str | None = None,
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

    ``event_key`` is the key the decision fired (C-033 round 2): two fan-out
    siblings can share the raw ``tcid``, and the gate that was decided is the
    one that waits on that key. It selects the gate when it names one; the raw
    id decides only for a key-less drain or a key that names none.

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
        {"graph_checkpoint": checkpoint}, tool_call_id=tcid, kind="_approval", event_key=event_key,
    )
    if gate is None:
        return
    decision, reason, _kind = classify_approval_payload(payload)
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


async def _write_graph_end(pool: "WorkerPool", session, *, reason: str, executor=None) -> None:
    """Append the graph's own end record (:mod:`primer.session.graph_end`) to the session's log, so the turn this resume finishes is a CLOSED window.

    A graph's stream ends with no terminal of its own and this path ends the session through the pool, which writes no record either; dispatch's clean completion writes the end of a run that
    never parked, and a run that did park and is resumed here ends nowhere else. ``reason`` is the one the session is about to be ended with (``completed`` or ``failed``), and the record says
    the same, unless ``executor`` is given and reports how the graph really ended (``last_done_reason``): then the record is the executor's truth (the row's reason for a drained resume is a
    known, separate ticket: it ends ``completed`` whatever the graph's own ended reason was). Seeded from a FRESH row, like the sibling record writers above and below it (``fresh_session_row_and_last_seq``). Best-effort: a write that fails is logged and the session still
    ends, as a lost record has never been allowed to keep one alive.
    """
    try:
        if getattr(pool, "_storage", None) is None:
            return
        from primer.model.workspace_session import WorkspaceSession
        from primer.session.graph_end import graph_end_for, graph_end_record
        from primer.session.persistence import WorkspaceMessageWriter
        from primer.session.seq_reservation import advance_last_seq
        from primer.worker.graph_resume import fresh_session_row_and_last_seq

        _fresh, last_seq = await fresh_session_row_and_last_seq(pool, session)
        ws = await pool._load_workspace_for_persist(session.workspace_id)
        writer = WorkspaceMessageWriter(workspace_io=ws, session_id=session.id, start_seq=last_seq)
        record = graph_end_for(getattr(executor, "last_done_reason", None), executor) if executor is not None else None
        new_seq = await writer.append(record or graph_end_record(failed=reason != "completed", ended_reason=reason))
        await writer.flush()
        # ONE field-scoped patch of last_seq (it only advances), not a whole-document update of a row that may have moved: the next writer seeds from it.
        await advance_last_seq(pool._storage.get_storage(WorkspaceSession), session.id, new_seq)
        if getattr(pool, "_event_bus", None) is not None:
            await pool._event_bus.publish(f"session:{session.id}:tick", {"seq": new_seq})
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        logger.exception("resume: failed to write the graph's end record for session %s", session.id)


async def end_graph(pool: "WorkerPool", session, *, reason: str, executor=None):
    """Close the graph's turn (its end record), then end the session with ``reason``: the order matters, because ending the session can realize a queued steer that reopens it.

    Every coordinator that ends a GRAPH session goes through here (this module's resume and ``tool_wait_resume_coordinator.resume_graph_tool_wait``); a path that does not leaves the window open until the
    next reopen's divider closes it."""
    await _write_graph_end(pool, session, reason=reason, executor=executor)
    return await pool._end_session(session, reason=reason)


def _wake_gate_id(raw_payload) -> str | None:
    """The gate a human decision's wake names (:data:`~primer.model.yield_.WAKE_GATE_ID_KEY`), or ``None`` (a wake from before gates had ids, a machine wake, a reply that is not a dict)."""
    from primer.model.yield_ import WAKE_GATE_ID_KEY

    named = raw_payload.get(WAKE_GATE_ID_KEY) if isinstance(raw_payload, dict) else None
    return named if isinstance(named, str) and named else None


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
        return await end_graph(pool, session, reason="failed")

    resume_payload = classify_resume_payload(parked, parked_at=session.parked_at)
    workspace = await pool._load_workspace_for_persist(session.workspace_id)
    try:
        executor_or_driver = await pool._build_graph_executor(session, workspace)
    except Exception:
        logger.exception(
            "resume: failed to build graph executor for session %s -"
            " ending failed", sid,
        )
        return await end_graph(pool, session, reason="failed")
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
    # on this path - never from the dict key itself. Both paths hand a
    # node the CLASSIFIED payload (a timeout or cancel marker becomes
    # YieldTimeout / YieldCancelled). A single-event park uses the singular
    # path (resumed_tcid from the fired key, or None for the legacy
    # drain-all).
    raw_state = session.parked_state or {}
    payloads_map = raw_state.get("resume_event_payloads")
    ck = parked.graph_checkpoint
    if payloads_map:
        # 7a gate review (verdict item 4): a co-pending tool_wait batch's
        # OWN wake rides in this SAME accumulation dict (multi-event
        # parks don't distinguish "human reply" from "tool_wait wake" at
        # the session-row level - durably_mark_session_resumable
        # accumulates whichever key fired). Feeding a tool_wait entry's
        # {"tool_wait_ready": True} payload through classify_approval_
        # payload fails closed to "rejected" (no "decision" key), which
        # installs _rejecting_dispatch on the SHARED executor for the
        # REST of this loop - silently rejecting a genuinely approved
        # co-pending gate processed on a LATER iteration. tool_wait
        # batches are handled ENTIRELY by the readiness re-check inside
        # the loop below (independent of which reply fired); they must
        # never reach the approval-decision machinery at all. Recognized
        # by the shared tool_wait_event_key helper's own shape - the
        # "tool_wait:" prefix is reserved to it, never produced by any
        # human-gate event_key.
        #
        # S2a: ONE reply per event_key. A park written by an older build
        # holds a leaf under the RAW dispatch key; the durable flip now
        # writes leaves under an encoded key (leaf_key_for, ticket
        # 01a122cc-effa), so a resend or an echo of the same reply lands a second
        # entry under the encoded spelling. Keep the entry whose dict key IS the raw spelling (only
        # older code writes it, so it is the older entry). jsonb does not
        # keep key insertion order, so the order of .values() says nothing
        # about which entry is older.
        from primer.session.yields import _dispatch_key_for

        kept: dict[str, dict] = {}
        for dict_key, entry in payloads_map.items():
            entry = entry or {}
            event_key = entry.get("event_key", "")
            if event_key.startswith("tool_wait:"):
                continue
            if event_key not in kept or dict_key == _dispatch_key_for(
                event_key, session_id=sid,
            ):
                kept[event_key] = entry
        # Each kept payload is classified as the singular path classifies
        # its one payload: a __yield_timeout__ / __yield_cancelled__ marker
        # becomes YieldTimeout / YieldCancelled (elapsed from the park's
        # parked_at), a real reply loses the internal control keys.
        # ask_user's and the external tool's resume hooks recognise a marker
        # only by isinstance: a raw marker dict would reach them as an
        # operator reply with no response.
        replies = [
            (
                event_key.rsplit(":", 1)[-1] or None,
                classify_marker_payload(
                    entry.get("payload") or {}, parked_at=session.parked_at,
                ).payload,
                # The FIRED key rides with the reply (C-033 round 2): the raw tool_call_id alone cannot say which of two fan-out siblings it answers.
                event_key or None,
                # ... and so does the GATE the decision named, read from the raw wake because classifying strips it: two siblings can share the key, and the approval of one is not the approval of the other.
                _wake_gate_id(entry.get("payload")),
            )
            for event_key, entry in kept.items()
        ]
        if not replies:
            # Every accumulated reply this cycle was a tool_wait wake -
            # no human gate has been answered yet, but the co-pending
            # tool_wait readiness re-check below still needs to run
            # exactly once. A sentinel tcid matching no real
            # _PendingToolCall/_PendingAgentYield keeps this a genuine
            # no-op on the human-gate side (never None, which would
            # legacy-drain-all and spuriously answer the STILL-unanswered
            # gate); "approved" avoids classify_approval_payload's
            # fail-closed-to-rejected default installing the rejecting
            # override for no reason - mirrors resume_graph_tool_wait's
            # own identical convention for its no-real-approval-here case.
            replies = [("__tool_wait_wake_only__", {"decision": "approved"}, None, None)]
    else:
        resume_event_key = raw_state.get("resume_event_key")
        resumed_tcid = (
            resume_event_key.rsplit(":", 1)[-1] if resume_event_key else None
        )
        replies = [(resumed_tcid, resume_payload.payload, resume_event_key or None, _wake_gate_id(parked.resume_event_payload))]

    repark = None
    # 01a0690a piece 3: per-node mint-seq high-water mark, seeded from the
    # original park and advanced after each drain below -- threaded into
    # any repark this loop produces so a chain of resumes never re-mints a
    # colliding scoped id.
    node_tool_call_seq = dict(getattr(parked, "node_tool_call_seq", None) or {})
    for tcid, payload, fired_key, gate_id in replies:
        # Unified nested-yield: when the parked agent-node yielded from
        # INSIDE a nested invoke_agent invocation, its pending entry carries
        # a continuation ``frames`` stack. Run the continuation walk to
        # unwind the subagent chain into a single tool_result FIRST; deliver
        # that as the node's agent_tool_result (Deliver), or re-park the
        # graph session on the deeper new leaf if a frame re-yielded
        # (Repark). The no-nested-frames path below is UNCHANGED.
        nested = pool._graph_nested_agent_yield(ck, tcid, event_key=fired_key)
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
                session=session, checkpoint=ck, tcid=tcid, payload=payload, event_key=fired_key,
            )
            if cont.repark_outcome is not None:
                return cont.repark_outcome
            agent_tool_result = cont.agent_tool_result
        else:
            agent_tool_result = await pool._graph_agent_tool_result(
                ck, tcid, payload, session_id=session.id, event_key=fired_key,
            )
            # An approval gate is a pending tool-call yield (NOT an ask_user
            # agent yield, which carries agent_tool_result). Persist the
            # resolved decision for that gate exactly once per reply. A
            # value-yielding tool_call (ask_user) is NOT an approval gate:
            # its result is the operator's reply, fed back by the executor,
            # so skip the approval record for it.
            if agent_tool_result is None and not pool._graph_value_yield_toolcall(
                ck, tcid, event_key=fired_key,
            ):
                await pool._write_approval_record_for_graph(
                    session=session, checkpoint=ck, tcid=tcid, payload=payload, event_key=fired_key,
                )
        # 01a0690a piece 2: agent_tool_result is a synthesized delivery
        # (ask_user answer / unwound nested continuation) with no
        # StreamEvent of its own -- the only way it gets a durable display
        # record is an explicit write here.
        if agent_tool_result is not None:
            await pool._persist_resume_tool_result_record_for_graph(
                session=session, checkpoint=ck, tcid=tcid,
                agent_tool_result=agent_tool_result, event_key=fired_key,
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
                resumed_event_key=fired_key,
                resumed_gate_id=gate_id,
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
            return await end_graph(pool, session, reason="failed")
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
        from primer.session.persistence import TurnInvariantError

        try:
            return pool._repark_graph_outcome(
                session, repark, node_tool_call_seq=node_tool_call_seq,
            )
        except TurnInvariantError:
            logger.exception(
                "resume: graph session %s cannot re-park - ending failed", sid,
            )
            return await end_graph(pool, session, reason="failed")

    # Drained to completion (the graph's own state.json carries the
    # real ended_reason; the session row mirrors _GraphTurnDriver).
    return await end_graph(pool, session, reason="completed", executor=executor)


def graph_value_yield_toolcall(pool: "WorkerPool", checkpoint, tcid, event_key: str | None = None) -> bool:
    """True when ``tcid`` is a pending tool_call node that suspended on a
    value-yielding tool (e.g. ``ask_user``) rather than an approval gate.

    Such a node's result is the operator's reply (fed back by the executor
    via the tool's resume hook), so the worker must NOT write an approval
    record for it nor classify the reply as an approve/reject decision.
    """
    from primer.graph._node_refs import _PendingToolCall, _is_value_yield_toolcall
    from primer.session.pending_gates import pending_entries

    matches = pending_entries(checkpoint, "pending_toolcalls", tool_call_id=tcid, event_key=event_key)
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


def graph_nested_agent_yield(pool: "WorkerPool", checkpoint, tcid, event_key: str | None = None):
    """Return the parked agent-node entry for ``tcid`` IFF it carries a
    nested continuation ``frames`` stack, else ``None``.

    A non-empty ``frames`` marks a node that yielded from inside a nested
    ``system__invoke_agent`` invocation; those resume through the
    continuation walk (:meth:`_resume_graph_continuation`) rather than the
    flat ask_user / approval path.
    """
    from primer.session.pending_gates import pending_entries

    matches = pending_entries(checkpoint, "pending_agent_yields", tool_call_id=tcid, event_key=event_key)
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
        # The entry the reply resolved: its node and its event key. The raw tool_call_id is not unique (two fan-out siblings parked inside a nested
        # invoke_agent can share the outer one), and rewriting the first entry that carries it re-pointed the OTHER sibling at this node's deeper leaf.
        if e.get("node_id") == ay.get("node_id") and e.get("event_key") == ay.get("event_key"):
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


async def graph_agent_tool_result(
    pool: "WorkerPool", checkpoint, tcid, payload, *, session_id: str, event_key: str | None = None,
):
    """Build the tool_result Message an agent-node yield continues from
    (e.g. the ask_user answer). Returns None for tool_call approvals /
    agent-node approvals (those take the bypass/verdict path) or when
    the fired tcid is not a hook-backed agent yield.

    The hook gets the same :class:`ResumeContext` the agent-session path
    builds (session_resume_coordinator.py): every registered hook takes
    three arguments, and a two-argument call raised a TypeError that the
    handler below turned into a "resume failed" result, losing every
    plain agent-node ask_user answer and external-tool reply.
    ``session_id`` is always the session being resumed: the engine resume
    passes ``session.id`` and the GraphFrame leaf path reaches here through
    the closure ``build_invocation_services`` binds to its session."""
    from primer.model.chat import Message, ToolResultPart
    from primer.session.pending_gates import pending_entries

    matches = pending_entries(checkpoint, "pending_agent_yields", tool_call_id=tcid, event_key=event_key)
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
        registry = getattr(pool, "_provider_registry", None)
        hook_result = hook(
            ay.get("resume_metadata") or {},
            payload,
            ResumeContext(
                tool_name=ay["tool_name"],
                tool_call_id=ay["tool_call_id"],
                session_id=session_id,
                resolve_provider=registry.get_toolset if registry is not None else None,
            ),
        )
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
    pool: "WorkerPool", *, session, checkpoint, tcid, agent_tool_result, event_key: str | None = None,
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

    A tool_call NODE's resume (approval, or ask_user answered via a
    _PendingToolCall, not a _PendingAgentYield) has no gap this function
    fills: the drain's own events carry its answer. The node wrote its
    ToolCallStart/End before it parked (live, through the dispatch tap), and
    the resume drain yields the _ExecutorToolResult for that row
    (primer.graph.base's resume loop, under the entry's row_call_id), which
    the resume-drain tap translates with the scoped id seeded from the
    checkpoint (primer.worker.graph_resume) -- that's why this only searches
    pending_agent_yields.

    Best-effort, same doctrine as the agent-path sibling: a write failure
    here must not fail an otherwise-successful resume.

    7a gate review (verdict R2-3): seeds the writer from a FRESH storage
    read of ``last_seq`` via the shared ``fresh_session_row_and_last_seq``
    helper, never the ``session`` object threaded down the call chain -
    this function runs in the SAME resume chain as ``resume_graph_from_
    checkpoint``'s own resume-drain tap (``_ResumeDrainTap``), which may
    already have advanced ``last_seq`` in storage by the time this runs.
    A stale seed produces a seq collision or gap; a stale ``model_copy``
    base would additionally roll back whatever OTHER fields the drain
    already wrote. This is the exact hazard item C fixed one call site
    over (``persist_resume_tool_result_records``) - this sibling missed
    it originally, which is why the helper is now shared.
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
    from primer.worker.graph_resume import fresh_session_row_and_last_seq

    tool_result_part = next(
        (p for p in agent_tool_result.parts if isinstance(p, ToolResultPart)),
        None,
    )
    if tool_result_part is None:
        return

    from primer.session.pending_gates import pending_entries

    node_id = None
    scoped_call_id = None
    for e in pending_entries(checkpoint, "pending_agent_yields", tool_call_id=tcid, event_key=event_key):
        node_id = e.get("node_id")
        scoped_call_id = e.get("scoped_tool_call_id")
        break

    try:
        fresh, last_seq = await fresh_session_row_and_last_seq(pool, session)
        ws = await pool._load_workspace_for_persist(session.workspace_id)
        writer = WorkspaceMessageWriter(
            workspace_io=ws, session_id=session.id, start_seq=last_seq,
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
        # Base the update on the FRESH row, not the stale `session`
        # parameter - see fresh_session_row_and_last_seq's own docstring.
        storage = pool._storage.get_storage(WorkspaceSession)
        await storage.update(fresh.model_copy(update={"last_seq": new_seq}))
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

    Itself a pure re-write, no row creation: any batch NEW to this
    repark (a resumed node's own continuation dispatching a further
    claims round - 7a gate review verdict R2-1) already had its
    ``ToolCallTask`` rows + claim-engine upserts materialized upstream,
    inside :func:`primer.worker.graph_resume.resume_graph_from_checkpoint`
    (the SAME shared ``materialize_pending_tool_wait_rows`` helper
    dispatch.py's own park catches use), before this function ever
    runs. A batch already carried over from an EARLIER park was
    materialized then, by dispatch.py's classic ``except YieldToWorker``
    branch. This just recomputes each
    batch's wake key (a pure function of the batch's ids - see
    ``tool_wait_event_key``) and writes a fresh ``ParkRequest`` pointing
    at the same already-claimable rows. Raises ``TurnInvariantError`` when
    there are pending batches and none of them has a parseable id (the
    resume coordinators end the session failed). ``node_tool_call_seq`` is threaded through unchanged for the
    same reason ``_repark_graph_yield_outcome`` does - a FURTHER resume
    of this repark (a node's own next dispatch round) must not re-mint a
    colliding scoped id.
    """
    from datetime import timedelta
    from primer.int.claim import ParkRequest, ReleaseOutcome
    from primer.model.tool_call_task import tool_call_task_id
    from primer.session.persistence import TurnInvariantError
    from primer.session.yields import tool_wait_event_key_or_none
    from primer.worker.yield_runtime import ToolWaitParkedState

    graph_checkpoint = repark.graph_checkpoint
    pending_tool_waits = list((graph_checkpoint or {}).get("pending_tool_waits") or [])
    # Each batch's key from the first id of the entry (a malformed one is logged, counted and left out); a park
    # with no wake key at all is never written (no parked_event_key means no timeout backstop either).
    candidate_keys = [
        tool_wait_event_key_or_none(
            session.id,
            scoped_task_id=(
                list(pw["outstanding_task_ids"])
                + [sid for sid, _ in pw["notifying_results"]]
            )[0],
            site="repark",
        )
        for pw in pending_tool_waits
    ]
    wake_keys = [key for key in candidate_keys if key is not None]
    if pending_tool_waits and not wake_keys:
        raise TurnInvariantError(
            f"session {session.id} graph re-park has no wake key: no pending batch's task id parses as a "
            "scoped tool-call id, and a park nothing can wake is never written"
        )
    now = datetime.now(timezone.utc)
    timeout = 3600.0
    # The park exception carries scoped call ids; the rows, leases and blobs use the session-qualified form.
    # The flat lists are the entries' own ids, in the form each entry stores (a carried-over entry parked before ids
    # were qualified keeps its bare ids, because its rows are under them). Only when there are no entries does the
    # exception's scoped ids get qualified here.
    if pending_tool_waits:
        flat_outstanding = [i for pw in pending_tool_waits for i in pw["outstanding_task_ids"]]
        flat_notifying = [sid for pw in pending_tool_waits for sid, _ in pw["notifying_results"]]
    else:
        flat_outstanding = [tool_call_task_id(session.id, i) for i in repark.outstanding_task_ids]
        flat_notifying = [tool_call_task_id(session.id, sid) for sid, _ in repark.notifying_results]
    parked_state = ToolWaitParkedState(
        outstanding_task_ids=flat_outstanding,
        notifying_task_ids=flat_notifying,
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
