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

from primer.model.yield_ import YieldToWorker
from primer.worker.yield_resume_registry import get_resume_hook
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

    Best-effort: a write failure is logged and swallowed so the resume
    proceeds. Shared by the agent and graph resume paths via the same
    parked-state blob shape.
    """
    from primer.agent.approval_record import (
        record_from_parked_blob,
        write_approval_record,
    )
    from primer.model.tool_approval import ToolApprovalRecord

    decision, reason = classify_approval_payload(payload)
    record = record_from_parked_blob(
        blob=blob,
        decision=decision,
        reason=reason,
        agent_id=getattr(session.binding, "agent_id", None),
        session_id=session.id,
        requested_at=session.parked_at,
    )
    storage = (
        pool._storage.get_storage(ToolApprovalRecord)
        if pool._storage is not None
        else None
    )
    await write_approval_record(storage, record)


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
    try:
        parked = ParkedState.from_jsonable(blob)
    except (KeyError, ValueError, TypeError):
        logger.exception(
            "resume: malformed parked_state for session %s - ending failed",
            sid,
        )
        return await pool._end_session(session, reason="failed")

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
        try:
            outcome = await resume_continuation(
                parked.frames,
                parked.yielded,
                resume_payload.payload,
                services,
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
            hook_result = hook(parked.yielded.resume_metadata, resume_payload.payload)
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

    # Continuation: clear park (on_release) + keep the lease so the next
    # claim runs the continuation LLM turn.
    return ReleaseOutcome(success=True, drop_lease=False)


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

    async def graph_agent_tool_result(checkpoint, tcid, payload):
        # Reuse the worker's own helper so a GraphFrame leaf resolves an
        # agent-node ask_user answer consistently.
        return await pool._graph_agent_tool_result(checkpoint, tcid, payload)

    return InvocationServices(
        build_subagent_toolmanager=build_subagent_toolmanager,
        resume_subagent=resume_subagent,
        resolve_graph=resolve_graph,
        build_child_graph_executor=build_child_graph_executor,
        graph_agent_tool_result=graph_agent_tool_result,
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
    new_parked = ParkedState(
        yielded=leaf,
        llm_messages=parked.llm_messages,
        turn_no=session.turn_no,
        started_at=now,
        tool_call_id=parked.tool_call_id,
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
