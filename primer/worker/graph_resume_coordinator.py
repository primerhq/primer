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

    The gated call's metadata lives on the checkpoint's
    ``pending_agent_yields`` entry for ``tcid``. We reshape it into the
    ``parked_state`` blob form so the shared builder applies. Best-effort:
    a missing entry or write failure is logged + swallowed.
    """
    from primer.agent.approval_record import (
        record_from_parked_blob,
        write_approval_record,
    )
    from primer.model.tool_approval import ToolApprovalRecord

    # Two gate shapes resolve here. An agent-node ``_approval`` yield lives
    # in ``pending_agent_yields`` and carries its own resume_metadata. A
    # tool-call-node gate lives in ``pending_toolcalls`` and its
    # ``original_call`` (tool_id + arguments) is denormalised into
    # ``pending_dispatch``. Either way, reshape into the parked_state blob
    # form the shared builder expects. A tcid that matches neither (or no
    # tcid at all -> legacy drain) is skipped.
    resume_metadata: dict | None = None
    entry = next(
        (e for e in (checkpoint.get("pending_agent_yields") or [])
         if e.get("tool_call_id") == tcid),
        None,
    )
    if entry is not None and entry.get("tool_name") == "_approval":
        resume_metadata = entry.get("resume_metadata") or {}
    else:
        disp = next(
            (d for d in (checkpoint.get("pending_dispatch") or [])
             if d.get("tool_call_id") == tcid),
            None,
        )
        if disp is not None:
            resume_metadata = disp.get("resume_metadata") or {}
    if resume_metadata is None:
        return
    decision, reason = classify_approval_payload(payload)
    blob = {
        "tool_call_id": tcid,
        "yielded": {"resume_metadata": resume_metadata},
    }
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


async def resume_graph_engine(pool: "WorkerPool", session, parked):
    """Resume a graph-bound session parked at a ToolCall approval.

    Adapted from the (dead) _handle_graph_resume: always terminal (graph
    sessions run to completion in one resume), so this returns a drop-lease
    outcome with ENDED status written to the row."""
    from primer.worker.graph_resume import resume_graph_from_checkpoint

    sid = session.id
    assert parked.graph_checkpoint is not None

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
    # reply that arrived into ``resume_event_payloads`` (tcid -> reply);
    # we drain them ALL so a concurrent second reply isn't lost. A
    # single-event park / timeout / cancel uses the singular path
    # (classified payload, resumed_tcid from the fired key, or None for
    # the legacy drain-all).
    raw_state = session.parked_state or {}
    payloads_map = raw_state.get("resume_event_payloads")
    ck = parked.graph_checkpoint
    if payloads_map:
        replies = [
            (tcid, (entry or {}).get("payload") or {})
            for tcid, entry in payloads_map.items()
        ]
    else:
        resume_event_key = raw_state.get("resume_event_key")
        resumed_tcid = (
            resume_event_key.rsplit(":", 1)[-1] if resume_event_key else None
        )
        replies = [(resumed_tcid, resume_payload.payload)]

    repark = None
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
        try:
            _decision, repark = await resume_graph_from_checkpoint(
                executor=executor,
                checkpoint=ck,
                payload=payload,
                resumed_tcid=tcid,
                agent_tool_result=agent_tool_result,
            )
        except Exception:
            logger.exception(
                "resume: graph executor for session %s raised during"
                " resume drain - ending failed", sid,
            )
            return await pool._end_session(session, reason="failed")
        if repark is None:
            break  # graph drained to completion
        ck = repark.graph_checkpoint  # resume the next reply from here

    if repark is not None:
        # Human-interaction nodes still pending (not yet replied to) ->
        # re-park on the remaining keys (no re-dispatch).
        return pool._repark_graph_outcome(session, repark)

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

    raw = next(
        (e for e in (checkpoint.get("pending_toolcalls") or [])
         if e.get("tool_call_id") == tcid),
        None,
    )
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
    ay = next(
        (e for e in (checkpoint.get("pending_agent_yields") or [])
         if e.get("tool_call_id") == tcid),
        None,
    )
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

    ay = next(
        (e for e in (checkpoint.get("pending_agent_yields") or [])
         if e.get("tool_call_id") == tcid),
        None,
    )
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


def repark_graph_outcome(pool: "WorkerPool", session, repark):
    """Build a ReleaseOutcome that re-parks a graph session on the
    remaining human-interaction keys after one reply was resumed."""
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
