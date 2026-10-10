"""Agent-session resume / repark coordinator for the worker pool.

Extracted verbatim from :mod:`primer.worker.pool` (no behaviour change). The
agent-session resume cluster drives an *agent*-bound session parked at a
human-interaction point (ToolCall approval / ask_user / nested invoke_agent
yield) back to completion or re-parks it on the remaining keys. The sibling
:mod:`primer.worker.graph_resume_coordinator` handles the graph-bound branch;
``resume_engine_session`` dispatches to it via ``pool._resume_graph_engine``.

Each function takes the :class:`~primer.worker.pool.WorkerPool` instance as
``pool`` and reads / calls the same bound deps and sibling methods the original
``WorkerPool`` methods did (``pool._storage``, ``pool._end_session``,
``pool._build_agent_executor``, ``pool._resume_graph_engine``, ...). The pool
keeps thin delegating methods so call sites and test monkeypatches still
resolve through the instance: when one routine calls another (e.g.
``pool._inject_resume_and_continue``) it dispatches through the patchable
instance method.

Lazy imports inside each function preserve the original module's tiny import
surface (the worker pool imported these dependencies inside the methods).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from primer.model.yield_ import YieldToWorker, wake_gate_id_of

# Imported for its side effect: registers the ``_external`` resume hook.
# The provider module is otherwise imported lazily (only when a caller
# registers external tools), so a worker process resuming a park it
# never created (e.g. after a restart) would miss the hook without this.
import primer.agent.external_tools  # noqa: F401
from primer.worker.yield_resume_registry import (
    ResumeContext,
    get_resume_hook,
)
from primer.worker.yield_runtime import (
    _resume_tool_approval,
    classify_approval_payload,
    classify_resume_payload,
    ParkedState,
)

if TYPE_CHECKING:
    from primer.worker.pool import WorkerPool

logger = logging.getLogger(__name__)


async def write_approval_record_for_session(
    pool: "WorkerPool", *, session, blob: dict, payload,
) -> None:
    """Persist the resolved approval decision for a session park.

    01a068da: this used to be the ONLY write site (a crash between an
    operator's respond and this eventual resume lost the audit record
    entirely). The respond route (tool_approval.py's _publish_decision)
    now writes the SAME record durably at the moment the decision
    arrives, keyed by gate_event_key, so this call is a FALLBACK: it
    still fires unconditionally on every resume (a synthesised timeout/
    cancel verdict never goes through respond at all, so this remains
    the ONLY write site for those), but when the respond-time write
    already landed, gate_event_key's unique index turns this insert into
    an expected no-op rather than a duplicate row (see
    write_approval_record's own docstring).

    Best-effort: a write failure is logged and swallowed so the resume
    proceeds.

    01a07be5 gate-review-2 finding 2: always passes
    ``warn_on_decision_mismatch=True`` -- see
    ``write_approval_record_for_graph``'s matching docstring note. The
    flip-winner and record-winner are independent races even for a real
    operator decision (not just a synthesised timeout/cancel), and this
    write is the one place that knows the decision that ACTUALLY resumed
    the park.
    """
    from primer.agent.approval_record import (
        record_from_parked_blob,
        write_approval_record,
    )
    from primer.model.tool_approval import ToolApprovalRecord

    decision, reason, _kind = classify_approval_payload(payload)
    blob = _blob_of_the_answered_gate(blob)
    yielded: dict = blob.get("yielded") or {}
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
        gate_event_key=yielded.get("event_key"),
    )
    storage = (
        pool._storage.get_storage(ToolApprovalRecord)
        if pool._storage is not None
        else None
    )
    await write_approval_record(
        storage, record, warn_on_decision_mismatch=True,
    )


def _blob_of_the_answered_gate(blob: dict) -> dict:
    """``blob`` with ``yielded`` / ``tool_call_id`` replaced by the pending gate the reply ANSWERED, when that is not the top-level one.

    An agent session parked on an ``invoke_graph`` child carries the child's PRIMARY gate as ``yielded`` and the whole child checkpoint in ``graph_checkpoint``;
    every pending gate of the child can be answered, and the reply stamps the key it fired as ``resume_event_key``. The audit record is built from the entry
    that key names (its own metadata, its own event key and gate id), never from the primary: a decision on B must not be recorded as a decision on A. A park
    with no checkpoint, no fired key, or a key that names no pending entry is returned as it was.

    The entry is looked up in the checkpoint the RESUME reads: the innermost ``GraphFrame``'s (``GraphFrame.answered_entry``), not the blob's top-level one, so the
    record and the gate that runs cannot name different entries (C-033 round 4). A re-park written before the top-level checkpoint was carried has none, or a stale
    one; the frame's is always the child's current state.
    """
    fired = blob.get("resume_event_key")
    checkpoint = blob.get("graph_checkpoint")
    frames = blob.get("frames") or []
    inner = frames[-1] if frames else None
    if isinstance(inner, dict) and inner.get("kind") == "graph" and inner.get("checkpoint"):
        checkpoint = inner["checkpoint"]
    if not fired or not checkpoint:
        return blob
    from primer.session.pending_gates import enumerate_pending_gates

    for entry in enumerate_pending_gates({"graph_checkpoint": checkpoint}):
        if entry.get("event_key") == fired:
            return {
                **blob,
                "yielded": {"tool_name": entry.get("kind"), "event_key": fired, "resume_metadata": entry.get("resume_metadata") or {}},
                "tool_call_id": entry.get("tool_call_id") or blob.get("tool_call_id"),
            }
    return blob


def _leaf_is_an_approval_gate(pool: "WorkerPool", parked: "ParkedState", fired_key: str | None = None) -> bool:
    """Whether the park's leaf is a real approval gate, so that answering it writes a ``ToolApprovalRecord``.

    ``_approval`` is the label EVERY graph park carries (``primer/graph/_checkpoint.py``), whatever its leaf is: an
    agent session parked on a child graph's ``ask_user`` node has ``parked.yielded.tool_name == "_approval"`` and its
    reply is an operator's answer, not an approve/reject decision (``classify_approval_payload`` would call it
    rejected and an audit record of a REJECTED approval would be written for it). When the INNERMOST frame is a
    ``GraphFrame`` the child checkpoint's entry for the node that yielded (``node_tcid``) says what the leaf is: a
    value-yielding ``tool_call`` node or a parked agent node whose own ``tool_name`` is not ``_approval`` is not an
    approval gate. Everything else keeps the label's meaning: a flat session park, a gate inside a nested agent chain,
    a child graph's real approval gate, and a frame whose node has no matching entry (nothing to tell it apart by).

    ``fired_key`` is the key the reply fired (the row's ``resume_event_key``): the leaf is the child's PRIMARY gate, and the reply may be for another
    sibling, so the entry it names is the one judged (an ask_user primary beside an approval sibling that was answered IS an approval).
    """
    if parked.yielded.tool_name != "_approval":
        return False
    from primer.worker.frames import GraphFrame

    inner = parked.frames[-1] if parked.frames else None
    if isinstance(inner, GraphFrame) and inner.node_tcid is not None:
        from primer.worker.graph_resume_coordinator import graph_value_yield_toolcall

        from primer.session.pending_gates import pending_entries

        checkpoint = inner.checkpoint or {}
        # The entry that was ANSWERED picks what is judged: two siblings of the child's superstep can share ``node_tcid``, and the first one with the raw id
        # is not necessarily the gate this reply answers (an ask_user sibling made a real approval look like a non-approval, and no record was written);
        # the leaf is only the child's primary, so the fired key comes first.
        node_tcid, leaf_key = inner.answered_entry(parked.yielded, fired_key)
        if graph_value_yield_toolcall(pool, checkpoint, node_tcid, event_key=leaf_key):
            return False
        for entry in pending_entries(checkpoint, "pending_agent_yields", tool_call_id=node_tcid, event_key=leaf_key)[:1]:
            if entry.get("tool_name") not in (None, "_approval"):
                return False
    return True


async def resume_engine_session(pool: "WorkerPool", engine_lease, session):
    """Drive a resumable park to its conclusion on the engine path.

    Engine-native resume dispatch: rehydrate the park, run the resume
    hook, inject the result, and return a ReleaseOutcome (no scheduler
    involvement):
      * agent success -> ReleaseOutcome(success=True, drop_lease=False):
        on_release clears the park columns + bumps turn_no, the lease is
        kept so the next claim runs the continuation LLM turn.
      * fail-closed   -> _end_session(reason='failed') (drop_lease=True).
    """
    import json
    from primer.model.chat import ToolResultPart

    sid = session.id
    blob = session.parked_state or {}
    # Phase 3 stage 7a (01a0518b) tripwire: a tool_wait park is not
    # Yielded-shaped (ParkedState.from_jsonable below assumes exactly
    # that shape) and must never reach this function - routing lives in
    # primer.worker.pool.WorkerPool._select_resume_handler, which peeks
    # this SAME key before ever calling here. This is a belt-and-braces
    # guard for if that routing is ever bypassed: fail LOUD and
    # immediately, not via the generic malformed-blob except below
    # (which would silently end the session as "failed" - a real bug
    # wearing the costume of an ordinary parked-state corruption).
    if blob.get("kind") == "tool_wait":
        raise RuntimeError(
            f"resume_engine_session: session {sid!r} has a tool_wait park "
            "- this must route through "
            "primer.worker.tool_wait_resume_coordinator.resume_engine_tool_wait "
            "instead (see WorkerPool._select_resume_handler); reaching here "
            "means that routing was bypassed"
        )
    try:
        parked = ParkedState.from_jsonable(blob)
    except (KeyError, ValueError, TypeError):
        logger.exception(
            "resume: malformed parked_state for session %s - ending failed",
            sid,
        )
        return await pool._end_session(session, reason="failed")

    # A switch applied while this session waited replaced the binding the
    # park belongs to. Continuing would run the outgoing agent's
    # half-finished turn under whoever holds the session now, so the
    # resume is voided instead. The switch already moved the row, and the
    # next turn starts cleanly under the current binding.
    if (
        parked.binding_epoch is not None
        and parked.binding_epoch != session.binding_epoch
    ):
        logger.info(
            "resume: voiding session %s parked at epoch %s (row is at "
            "epoch %s); the binding was switched while it waited",
            sid, parked.binding_epoch, session.binding_epoch,
        )
        return await pool._end_session(session, reason="cancelled")

    from primer.events.recorder import recorder_for

    await recorder_for(pool._storage, pool._event_bus).emit(
        "session.resumed",
        workspace_id=session.workspace_id,
        session_id=sid,
        payload={
            "event_key": getattr(
                getattr(parked, "yielded", None), "event_key", None,
            ),
        },
    )

    if session.binding.kind == "graph":
        if parked.graph_checkpoint is None:
            logger.error(
                "resume: graph session %s parked without a graph_checkpoint"
                " - ending failed", sid,
            )
            return await pool._end_session(session, reason="failed")
        return await pool._resume_graph_engine(session, parked)

    if session.binding.kind != "agent":
        logger.error(
            "resume: unsupported binding kind %r for session %s - ending"
            " failed", session.binding.kind, sid,
        )
        return await pool._end_session(session, reason="failed")

    if session.parked_at is None:
        logger.error(
            "resume: session %s resumable but parked_at=None - ending failed",
            sid,
        )
        return await pool._end_session(session, reason="failed")

    resume_payload = classify_resume_payload(parked, parked_at=session.parked_at)

    workspace = await pool._load_workspace_for_persist(session.workspace_id)
    executor_or_driver = await pool._build_agent_executor(session, workspace)
    executor = getattr(executor_or_driver, "_executor", executor_or_driver)
    tool_manager = getattr(executor, "_tool_manager", None)

    # Unified nested-yield continuation: a non-empty frame stack means the
    # leaf yield was raised INSIDE a nested invoke_agent invocation (the
    # session's own turn is NOT a frame - it lives in ``parked.llm_messages``
    # and is resumed by the shared inject tail below). Walk the frames to
    # resolve the leaf and unwind the chain into a single tool_result, then
    # fall through to the SAME inject/continue tail the per-tool_name path
    # uses. An empty stack routes to the existing switch UNCHANGED, which
    # preserves the persist-approvals decision-record writes and the
    # invoke_graph regression until task 5.1 migrates them.
    if parked.frames:
        from primer.worker.continuation import Repark, resume_continuation

        services = pool._build_invocation_services(
            session, workspace, executor, tool_manager,
        )
        # 01a07be5 gate-review-2 finding 1: the leaf gate this frames
        # stack unwinds can itself be an approval gate (a tool call
        # somewhere inside a nested invoke_agent chain that was gated).
        # parked.yielded IS that leaf here -- frames/leaf bookkeeping is
        # orthogonal identity plumbing, not a different object -- so the
        # SAME write the non-nested branch below uses applies unchanged.
        # Before this fix, taking this branch skipped the write entirely:
        # real decisions AND terminal synthesis for a nested approval
        # gate left no audit record at all, silently.
        fired_key = blob.get("resume_event_key")        # stamped by the flip with the payload: the key the reply FIRED
        if _leaf_is_an_approval_gate(pool, parked, fired_key=fired_key):
            await pool._write_approval_record_for_session(
                session=session, blob=blob, payload=resume_payload.payload,
            )
        # the gate the decision named, read from the RAW wake: the payload below is the classified one and has lost it (security review of #724, round 3, B1-r2a)
        wake_gate = wake_gate_id_of(parked.resume_event_payload)
        try:
            outcome = await resume_continuation(
                parked.frames,
                parked.yielded,
                resume_payload.payload,
                services,
                fired_key=fired_key,
                **({"gate_id": wake_gate} if wake_gate is not None else {}),
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed synthesis
            logger.exception(
                "resume: continuation walk for session %s raised;"
                " synthesising error tool_result", sid,
            )
            tool_result_part = ToolResultPart(
                id=parked.tool_call_id or "unknown",
                output=json.dumps({
                    "rejected": True,
                    "reason": (
                        f"continuation resume failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "tool_name": parked.yielded.tool_name,
                }),
                error=True,
            )
        else:
            if isinstance(outcome, Repark):
                # A frame (or the leaf re-dispatch) raised a fresh yield
                # mid-unwind -> re-park the reconstructed stack + new leaf.
                return pool._repark_continuation(session, parked, outcome)
            tool_result_part = outcome.tool_result
        return await pool._inject_resume_and_continue(
            session, executor, parked, tool_result_part,
        )

    tool_name = parked.yielded.tool_name
    try:
        if tool_name == "_approval":
            # Persist the resolved decision (approved/rejected/timeout/
            # cancelled) exactly once, BEFORE we re-dispatch/synthesise.
            # classify_approval_payload is the same classifier the resume
            # uses, so the record's verdict cannot drift from the result.
            await pool._write_approval_record_for_session(
                session=session, blob=blob, payload=resume_payload.payload,
            )
            tool_result_part = await _resume_tool_approval(
                blob=blob,
                payload=resume_payload.payload,
                tool_manager=tool_manager,
            )
        else:
            hook = get_resume_hook(tool_name)
            registry = getattr(pool, "_provider_registry", None)
            hook_result = hook(
                parked.yielded.resume_metadata,
                resume_payload.payload,
                ResumeContext(
                    tool_name=tool_name,
                    tool_call_id=parked.tool_call_id or "unknown",
                    session_id=getattr(session, "id", None),
                    resolve_provider=(
                        registry.get_toolset if registry is not None else None
                    ),
                ),
            )
            if asyncio.iscoroutine(hook_result):
                hook_result = await hook_result
            tool_result_part = ToolResultPart(
                id=parked.tool_call_id or "unknown",
                output=hook_result.output,
                error=hook_result.is_error,
            )
    except YieldToWorker as yld:
        # Two-phase park: an approval gate sat on a *yielding* tool. The
        # operator just APPROVED (phase 1), so _resume_tool_approval
        # re-dispatched the real tool with bypass_approval=True - which
        # itself yields for its own event (timer/file/graph/human). Do
        # NOT swallow this as an error: re-park the session on the new
        # event key (phase 2), preserving the in-progress turn messages,
        # so it resumes when the real event fires. Mirrors the normal
        # park path in primer/session/dispatch.py and
        # _repark_continuation below.
        return pool._repark_resumed_yield_outcome(session, parked, yld)
    except Exception as exc:  # noqa: BLE001 - fail-closed synthesis
        logger.exception(
            "resume: hook for tool %r on session %s raised; synthesising"
            " error tool_result", tool_name, sid,
        )
        tool_result_part = ToolResultPart(
            id=parked.tool_call_id or "unknown",
            output=json.dumps({
                "rejected": True,
                "reason": f"resume failed: {type(exc).__name__}: {exc}",
                "tool_name": tool_name,
            }),
            error=True,
        )

    return await pool._inject_resume_and_continue(
        session, executor, parked, tool_result_part,
    )


async def inject_resume_and_continue(
    pool: "WorkerPool", session, executor, parked, tool_result_part,
):
    """Inject the resolved tool_result into the parked turn + continue.

    Shared tail for BOTH the per-tool_name resume switch and the new
    nested-yield continuation walk: rehydrate the parked turn's assistant
    history, append the resolved ``tool_result_part`` as a tool message,
    persist them via ``inject_resume_messages``, and return the
    keep-the-lease continuation outcome (the next claim runs the
    continuation LLM turn). On persist failure it fails the session.
    """
    from primer.int.claim import ReleaseOutcome
    from primer.model.chat import Message

    rehydrated_assistant = [Message.model_validate(m) for m in parked.llm_messages]
    tool_result_msg = Message(role="tool", parts=[tool_result_part])
    try:
        await executor.inject_resume_messages(
            [*rehydrated_assistant, tool_result_msg],
        )
    except Exception:
        logger.exception(
            "resume: persist failed for session %s - ending failed",
            session.id,
        )
        return await pool._end_session(session, reason="failed")

    await _persist_resume_tool_result_record(pool, session, parked, tool_result_part)
    await _mark_resume_applied(pool, session)

    # Continuation: clear park (on_release) + keep the lease so the next
    # claim runs the continuation LLM turn.
    return ReleaseOutcome(success=True, drop_lease=False)


def resume_already_applied(session) -> bool:
    """Whether the park on ``session`` was already resumed, its release never having committed (ticket 01a10b54-425b).

    ``resumed_park_at`` is written by the continue path of :func:`inject_resume_and_continue` once the resume's effects are
    committed, and the release that follows clears the park. A row that still carries the park AND a marker naming it is
    exactly a release that was abandoned or rolled back: the pool's resume branch must not run the handler again (it would
    inject the reply a second time and re-run an approved tool). A marker that differs from ``parked_at`` names an older
    park (a later park stamps a new ``parked_at``) and means nothing.
    """
    return session.resumed_park_at is not None and session.resumed_park_at == session.parked_at


async def _mark_resume_applied(pool: "WorkerPool", session) -> None:
    """Record that the park on ``session`` was resumed (``WorkspaceSession.resumed_park_at``).

    One field-scoped ``patch_if`` fenced on what the marker NAMES: the park (``parked_at``) still on the row, still
    ``resumable``. It writes nothing else, and nothing once the release committed (the park columns are cleared) or the
    row parked again (a new ``parked_at``). Best-effort, like ``completed_turn_no``: a rejected fence or a storage error is
    logged and never fails the resume (without the marker a rolled-back release runs the handler again, as it did before).
    The record write before it is field-scoped as well (``advance_last_seq``): neither write overwrites the other, and the marker
    is still the last write, after every effect.

    Only the continue path calls this, so only a resume that continues is covered. NOT covered (ticket 01a1206e-0acc): a resume
    that RE-PARKS (a yielding tool behind an approval, a graph that still has pending siblings) and a handler that raises, whose
    effects are not all committed before the release, so a rollback of that release still runs the handler again; and
    ``tool_wait`` parks, which resume through their own coordinator. An exit through ``pool._end_session`` (ENDED, failed) is NOT
    a gap: it commits ENDED before the release, so the re-claim finds a finished session.
    """
    if pool._storage is None or session.parked_at is None:
        return
    from pydantic_core import to_jsonable_python

    from primer.model.workspace_session import WorkspaceSession
    from primer.storage import raw_generation

    storage = pool._storage.get_storage(WorkspaceSession)
    try:
        written = await storage.patch_if(
            session.id,
            to_jsonable_python({"resumed_park_at": session.parked_at}),
            where={"parked_at": [raw_generation(session, "parked_at")], "parked_status": ["resumable"]},
        )
    except Exception:  # noqa: BLE001 - best-effort; the resume's own outcome stands
        logger.warning(
            "resume: session %s: could not record the park as resumed; a rolled-back release would run the handler again",
            session.id, exc_info=True,
        )
        return
    if written is None:
        logger.warning(
            "resume: session %s: the park is no longer the one this resume ran for, so it is not recorded as resumed",
            session.id,
        )


async def _persist_resume_tool_result_record(
    pool: "WorkerPool", session, parked, tool_result_part,
) -> None:
    """Write the modern TOOL_RESULT counterpart for a resumed tool call.

    ``inject_resume_messages`` (above) only appends the legacy
    ``{role,parts}`` Message lines that feed LLM context reconstruction;
    the paired TOOL_CALL record was already written live before the park
    (``primer.session.persistence``'s ``ToolCallEnd`` handler), but
    nothing at park or resume time ever wrote the modern
    ``SessionMessageRecord`` TOOL_RESULT counterpart that the messages
    API + live tap read (01a04e0a). Payload shape mirrors the live-turn
    write (``call_id``/``output``/``error`` - see
    ``primer.session.persistence``'s ``_ExecutorToolResult`` handler),
    the same shape ``abandon_session_gate`` was fixed to use (01a05350)
    after ``primer.session.timeline`` was found unable to pair its old
    ``id``/``name``/``result`` shape back to a TOOL_CALL (it keys the
    lookup by ``call_id``).

    01a068ea-dc95: ``call_id`` uses ``parked.scoped_tool_call_id`` (the
    id the paired TOOL_CALL record actually carries -
    ``primer.session.persistence``'s ``ToolCallStart`` mint), falling
    back to ``tool_result_part.id`` (the raw provider id) only for a
    park written before this field existed. Using the raw id
    unconditionally (the pre-fix behaviour) left every resumed yield's
    TOOL_RESULT unpaired from its TOOL_CALL in the transcript/trace UI,
    since the durable pair never shared an id.

    Best-effort and best-effort ONLY: this is a secondary, display-side
    record. The turn's continuation already stands on the legacy write
    above having succeeded: a write failure here must not undo that or
    end an otherwise-healthy session (the transcript stays served by
    the UI's legacy-line tolerance in that case - see
    ``_dedupe_legacy_user_input``).
    """
    if pool._storage is None:
        return
    from primer.model.workspace_session import (
        SessionMessageKind,
        SessionMessageRecord,
        WorkspaceSession,
    )
    from primer.session.persistence import WorkspaceMessageWriter

    try:
        ws = await pool._load_workspace_for_persist(session.workspace_id)
        writer = WorkspaceMessageWriter(
            workspace_io=ws, session_id=session.id, start_seq=session.last_seq,
        )
        new_seq = await writer.append(SessionMessageRecord(
            seq=1,  # overwritten by the writer's monotonic counter
            kind=SessionMessageKind.TOOL_RESULT,
            payload={
                "call_id": parked.scoped_tool_call_id or tool_result_part.id,
                "output": tool_result_part.output,
                "error": tool_result_part.error,
            },
            created_at=datetime.now(timezone.utc),
        ))
        await writer.flush()
        # ONE field-scoped write of last_seq. The pool-start copy of the row this used to put back whole carried a
        # Stop the pool had just cleared, a steer's turn_status, and any marker written before it (#681 review, P8).
        from primer.session.seq_reservation import advance_last_seq

        await advance_last_seq(pool._storage.get_storage(WorkspaceSession), session.id, new_seq)
        # 01a068ea-dc95 (sibling finding, same shape as
        # claim/adapters/sessions.py's terminal-ERROR writer): a durable-
        # but-unticked write is invisible to a live client until its next
        # poll. Best-effort/advisory, same as every other tick publish in
        # this module - never lets a bus hiccup fail an otherwise-
        # successful write.
        if pool._event_bus is not None:
            try:
                await pool._event_bus.publish(
                    f"session:{session.id}:tick", {"seq": new_seq},
                )
            except Exception:  # noqa: BLE001 - advisory
                logger.exception(
                    "resume: tick publish failed for session %s", session.id,
                )
    except Exception:  # noqa: BLE001 - best-effort, see docstring
        logger.exception(
            "resume: failed to persist modern TOOL_RESULT record for "
            "session %s",
            session.id,
        )


def build_invocation_services(pool: "WorkerPool", session, workspace, executor, tool_manager):
    """Build the :class:`InvocationServices` bundle the continuation walk
    drives nested invocations through.

    Binds the worker's storage / provider-registry / approval-resolver into
    thin closures over :func:`primer.agent.invoke.build_subagent_toolmanager`
    and :func:`primer.agent.invoke.resume_subagent` (the same deps the worker
    wires a normal turn with), and threads the session's
    :class:`GraphInvocationServices` (off the tool_manager) for the
    graph-frame callables.
    The walk only ever calls these as ``services.<name>(...)``.
    """
    from primer.agent.invoke import (
        build_subagent_toolmanager as _build_subagent_toolmanager,
        resume_subagent as _resume_subagent,
    )
    from primer.worker.continuation import InvocationServices

    storage_provider = pool._storage
    provider_registry = pool._provider_registry
    approval_resolver = pool._approval_resolver

    async def build_subagent_toolmanager(context):
        return await _build_subagent_toolmanager(
            context,
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=approval_resolver,
        )

    async def resume_subagent(
        *, agent_id, context, llm_messages, child_result, depth,
        invoke_tool_call_id,
    ):
        return await _resume_subagent(
            agent_id=agent_id,
            context=context,
            llm_messages=llm_messages,
            child_result=child_result,
            depth=depth,
            storage_provider=storage_provider,
            provider_registry=provider_registry,
            approval_resolver=approval_resolver,
            invoke_tool_call_id=invoke_tool_call_id,
        )

    # Graph callables come off the session's GraphInvocationServices, which
    # the agent's tool_manager carries (set by _build_agent_executor); the
    # GraphFrame path that uses them only lands once task 5.1 migrates
    # invoke_graph onto the continuation walk. Bind defensively so an
    # absent bundle yields a clear error rather than an AttributeError.
    graph_services = getattr(tool_manager, "_graph_services", None)

    async def resolve_graph(graph_id):
        if graph_services is None:
            raise RuntimeError("graph services unavailable for this session")
        return await graph_services.resolve_graph(graph_id)

    async def build_child_graph_executor(graph, gsid):
        if graph_services is None:
            raise RuntimeError("graph services unavailable for this session")
        return await graph_services.build_child_executor(graph=graph, gsid=gsid)

    async def graph_agent_tool_result(checkpoint, tcid, payload, event_key=None):
        # Reuse the worker's own helper so a GraphFrame leaf resolves an
        # agent-node ask_user answer consistently. ``event_key`` is the
        # leaf's own: it picks the sibling when several share the raw id.
        return await pool._graph_agent_tool_result(
            checkpoint, tcid, payload, session_id=session.id, event_key=event_key,
        )

    async def resolve_provider(toolset_id):
        return await provider_registry.get_toolset(toolset_id)

    return InvocationServices(
        build_subagent_toolmanager=build_subagent_toolmanager,
        resume_subagent=resume_subagent,
        resolve_graph=resolve_graph,
        build_child_graph_executor=build_child_graph_executor,
        graph_agent_tool_result=graph_agent_tool_result,
        # The ResumeContext a GraphFrame leaf's value-yielding tool_call node
        # hands its resume hook: the session being resumed and the registry's
        # toolset resolver, as the agent-session resume builds them.
        session_id=getattr(session, "id", None),
        resolve_provider=resolve_provider if provider_registry is not None else None,
    )


def repark_continuation(pool: "WorkerPool", session, parked, outcome):
    """Re-park an AGENT session whose nested continuation re-yielded.

    A frame's resume (or the leaf re-dispatch) raised a fresh yield
    mid-unwind: the continuation walk returns a :class:`Repark` carrying the
    reconstructed (root-first) frame stack + the new innermost leaf. Persist
    a fresh :class:`ParkedState` whose ``frames`` is the reconstructed stack
    and whose ``yielded`` is the new leaf, preserving the SESSION turn's
    ``llm_messages`` + ``tool_call_id`` so the eventual completion pairs
    correctly.
    """
    from datetime import timedelta
    from primer.int.claim import ParkRequest, ReleaseOutcome

    leaf = outcome.leaf
    now = datetime.now(timezone.utc)
    timeout = leaf.timeout if leaf.timeout is not None else 3600.0
    # A session parked on an ``invoke_graph`` child keeps the child's whole state on the blob, as the first park (``dispatch.py``) writes it: the child's
    # checkpoint (every gate it still waits on, each with its own approvers) and the id of its new primary gate. The innermost frame is the child's, advanced
    # past the gate that was just answered (``GraphFrame._repark_with_advanced_frame``). A re-park that dropped them left the routes only the projection of the
    # primary, which carries no ``approvers``: the other gates were decided by anyone, or not at all (C-033 round 4).
    from primer.worker.frames import GraphFrame

    inner = outcome.frames[-1] if outcome.frames else None
    child_graph = isinstance(inner, GraphFrame)
    new_parked = ParkedState(
        yielded=leaf,
        llm_messages=parked.llm_messages,
        turn_no=session.turn_no,
        started_at=now,
        tool_call_id=(inner.node_tcid or parked.tool_call_id) if child_graph else parked.tool_call_id,
        graph_checkpoint=inner.checkpoint if child_graph else None,
        # 01a068ea-dc95: the leaf re-yielded, but the DURABLE TOOL_CALL
        # record this eventual TOOL_RESULT answers is still the outer
        # session-level call (parked.tool_call_id above) - carry its
        # scoped id forward unchanged, same reasoning as tool_call_id
        # itself.
        scoped_tool_call_id=parked.scoped_tool_call_id,
        frames=list(outcome.frames),
    )
    return ReleaseOutcome(
        success=True,
        drop_lease=True,
        park=ParkRequest(
            parked_state=new_parked.to_jsonable(),
            parked_event_key=leaf.event_key,
            parked_event_keys=getattr(leaf, "event_keys", None),
            parked_until=now + timedelta(seconds=timeout),
            parked_at=now,
        ),
    )


def repark_resumed_yield_outcome(pool: "WorkerPool", session, parked, yld):
    """Re-park an AGENT session whose approval-gated tool, once approved,
    yielded for its OWN real event (phase 2 of the two-phase park).

    Builds a fresh ParkedState from the re-raised YieldToWorker's event
    key / tool_call_id / resume_metadata, preserving the in-progress turn's
    rehydrated assistant messages so the eventual real-event resume pairs
    the tool_result against the original tool_use. Mirrors the normal park
    path in primer/session/dispatch.py and _repark_continuation.
    """
    from datetime import timedelta
    from primer.int.claim import ParkRequest, ReleaseOutcome

    yielded = yld.yielded
    now = datetime.now(timezone.utc)
    timeout = yielded.timeout if yielded.timeout is not None else 3600.0

    # Stamp parked_at_iso so the eventual resume hook can compute elapsed
    # without a separate read (mirrors dispatch.py's first-park path).
    resume_metadata = dict(yielded.resume_metadata)
    resume_metadata["parked_at_iso"] = now.isoformat()
    yielded_stamped = type(yielded)(
        tool_name=yielded.tool_name,
        event_key=yielded.event_key,
        timeout=yielded.timeout,
        resume_metadata=resume_metadata,
        event_keys=getattr(yielded, "event_keys", None),
    )

    new_parked = ParkedState(
        yielded=yielded_stamped,
        # Preserve the in-progress turn history (the assistant message that
        # emitted the original tool_use) so the real-event resume can pair
        # the synthesised tool_result against it.
        llm_messages=parked.llm_messages,
        turn_no=session.turn_no,
        started_at=now,
        tool_call_id=yld.tool_call_id,
        # 01a068ea-dc95: phase 2 re-dispatches the SAME tool call directly
        # (bypass_approval=True), outside the LLM-streaming pipeline that
        # mints scoped ids - there is no new one to look up, so carry the
        # phase-1 park's forward unchanged. It answers the same durable
        # TOOL_CALL record either way.
        scoped_tool_call_id=parked.scoped_tool_call_id,
        graph_checkpoint=getattr(yld, "graph_checkpoint", None),
    )
    return ReleaseOutcome(
        success=True,
        drop_lease=True,
        park=ParkRequest(
            parked_state=new_parked.to_jsonable(),
            parked_event_key=yielded_stamped.event_key,
            parked_event_keys=getattr(yielded_stamped, "event_keys", None),
            parked_until=now + timedelta(seconds=timeout),
            parked_at=now,
        ),
    )
