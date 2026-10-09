"""Resolve / cancel invoker-supplied tool calls for the session surface.

The steer endpoint delegates here so the chat surface and graph parks
can share the same atomic-validate-then-apply core. The park slot stays
the execution source of truth; the ``ExternalToolCall`` rows are the
API-facing record and are kept in lockstep by these helpers.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime
from typing import Any

from primer.model.except_ import ConflictError
from primer.model.external_tool import ExternalToolCall
from primer.model.yield_ import with_wake_entry, with_wake_park
from primer.model.storage import OffsetPage
from primer.session.external_calls import resolve_external_row
from primer.session.yields import durably_wake_session
from primer.storage.q import Q

logger = logging.getLogger(__name__)

CANCEL_REASON_SUPERSEDED = "superseded by new user message"


def _pending_targets(session: Any) -> dict[str, str]:
    """Map tool_call_id -> event_key for every external call parked on
    this session (single-agent park OR graph checkpoint entries).

    01a0518f: two concurrent fan-out siblings can share a raw provider
    tool_call_id; this dict collapses to whichever entry is written last,
    same as ``apply_tool_results``'s own ``rows``/``targets`` matching
    below (the ``ExternalToolResultIn`` wire contract has no other field
    to disambiguate - a full fix needs the delivered external id scoped
    and mapped back to the raw id server-side, tracked as a follow-up).
    Logged here so a real collision is visible instead of silent.
    """
    out: dict[str, str] = {}
    if getattr(session, "parked_status", None) not in ("parked", "resumable"):
        return out
    blob = session.parked_state or {}
    yielded = blob.get("yielded") or {}
    if yielded.get("tool_name") == "_external":
        tcid = blob.get("tool_call_id")
        key = yielded.get("event_key")
        if tcid and key:
            out[tcid] = key
    # Graph checkpoints carry external parks in two places, both keyed by
    # the "_external" marker name: tool-call node suspends live in
    # ``pending_toolcalls`` (with their wake key), agent-node yields in
    # ``pending_agent_yields`` (their event_key IS the wake key).
    checkpoint = blob.get("graph_checkpoint") or {}
    for entry in checkpoint.get("pending_toolcalls") or []:
        if entry.get("tool_name") != "_external":
            continue
        tcid = entry.get("tool_call_id")
        if not tcid:
            continue
        if tcid in out:
            logger.warning(
                "_pending_targets: tool_call_id=%r already pending on "
                "session %s; a later entry overwrites the earlier one",
                tcid, session.id,
            )
        out[tcid] = entry.get("parked_event_key") or (
            f"external_tool:{session.id}:{tcid}"
        )
    for entry in checkpoint.get("pending_agent_yields") or []:
        if entry.get("tool_name") != "_external":
            continue
        tcid = entry.get("tool_call_id")
        if not tcid:
            continue
        if tcid in out:
            logger.warning(
                "_pending_targets: tool_call_id=%r already pending on "
                "session %s; a later entry overwrites the earlier one",
                tcid, session.id,
            )
        out[tcid] = entry.get("event_key") or (
            f"external_tool:{session.id}:{tcid}"
        )
    return out


async def _rows_by_tcid(
    call_storage: Any,
    *,
    session_id: str | None = None,
    chat_id: str | None = None,
) -> dict[str, ExternalToolCall]:
    q = Q(ExternalToolCall).where("status", "pending")
    if session_id is not None:
        q = q.where("session_id", session_id)
    if chat_id is not None:
        q = q.where("chat_id", chat_id)
    page = await call_storage.find(q.build(), OffsetPage(offset=0, length=200))
    rows: dict[str, ExternalToolCall] = {}
    for r in page.items:
        if r.tool_call_id in rows:
            # 01a0518f: same wire-contract limit as _pending_targets above.
            logger.warning(
                "_rows_by_tcid: tool_call_id=%r already resolved to row "
                "%r; row %r overwrites it",
                r.tool_call_id, rows[r.tool_call_id].id, r.id,
            )
        rows[r.tool_call_id] = r
    return rows


async def apply_tool_results(
    session: Any,
    results: list,
    *,
    call_storage: Any,
    session_storage: Any,
    engine: Any,
    event_bus: Any,
    storage_provider: Any = None,
) -> int:
    """Validate ALL ids, then wake each matched park. 409-atomic.

    Raises :class:`ConflictError` before any state change if ANY id is
    unknown or already resolved. Returns the number of applied results.

    Each call's row is then marked ``completed`` through the guarded
    :func:`resolve_external_row`: a row that left ``pending`` after the
    validation (a cancel or the lazy timeout won the race) keeps the
    status it reached and is not overwritten.
    """
    targets = _pending_targets(session)
    rows = await _rows_by_tcid(call_storage, session_id=session.id)
    bad = [
        r.tool_call_id
        for r in results
        if r.tool_call_id not in targets or r.tool_call_id not in rows
    ]
    if bad:
        raise ConflictError(
            f"no pending external tool call(s) {bad!r} on session "
            f"{session.id!r}; nothing was applied"
        )
    for r in results:
        # The wake names the park this producer read (security ticket 01a1208d): delivered by key alone and at least once, a copy redelivered after the
        # session re-parked under the same key must not decide the new park.
        # ... and the call row it answers (``external_call_row_id`` of the pending entry), which survives a graph park's re-park where the stamp does not.
        payload = with_wake_entry(
            with_wake_park({"result": r.result, "is_error": bool(r.is_error)}, session.parked_at), rows[r.tool_call_id].id,
        )
        await durably_wake_session(
            session,
            event_key=targets[r.tool_call_id],
            payload=payload,
            session_storage=session_storage,
            engine=engine,
        )
        if storage_provider is not None:
            from primer.events.wake import emit_session_wake

            await emit_session_wake(
                storage_provider, event_bus,
                targets[r.tool_call_id], payload,
            )
        elif event_bus is not None:
            try:
                await event_bus.publish(targets[r.tool_call_id], payload)
            except Exception:  # noqa: BLE001 - durable flip already landed
                logger.exception("external tool result publish failed")
        landed = await resolve_external_row(
            call_storage, rows[r.tool_call_id].id,
            status="completed", result=r.result, is_error=bool(r.is_error),
        )
        if landed is None:
            logger.info(
                "external tool call %r on session %s left pending before its "
                "result was recorded; its row keeps the status it reached",
                r.tool_call_id, session.id,
            )
    return len(results)


async def cancel_pending_external(
    *,
    call_storage: Any,
    session_id: str | None = None,
    chat_id: str | None = None,
    reason: str = CANCEL_REASON_SUPERSEDED,
    tool_call_ids: Collection[str] | None = None,
    created_before: datetime | None = None,
) -> list[str]:
    """Cancel the owner's pending rows; return the tool_call_ids cancelled.

    Each row is cancelled by the guarded :func:`resolve_external_row`, so
    a row that left ``pending`` in the meantime (its result landed, or
    another cancel or the lazy timeout won) keeps its status and is not
    in the returned list: the list holds exactly the ids whose
    ``cancelled`` write landed.

    ``tool_call_ids`` restricts the cancel to those calls. ``created_before``
    (an aware datetime; a naive one raises ``ValueError`` before anything
    is read) restricts it to rows created strictly earlier, compared in
    Python; a row with no ``created_at`` is then spared.

    Row-side only: waking the park with the synthetic cancelled payload
    (so the turn resumes and pairs the call) is the caller's job, via
    the same wake helpers the results path uses.
    """
    if created_before is not None and created_before.utcoffset() is None:
        raise ValueError(
            "created_before must be a timezone-aware datetime: rows are "
            "compared on their aware created_at"
        )
    rows = await _rows_by_tcid(
        call_storage, session_id=session_id, chat_id=chat_id
    )
    wanted = None if tool_call_ids is None else set(tool_call_ids)
    landed: list[str] = []
    for tool_call_id, row in rows.items():
        if wanted is not None and tool_call_id not in wanted:
            continue
        if created_before is not None and (
            row.created_at is None or row.created_at >= created_before
        ):
            continue
        done = await resolve_external_row(
            call_storage, row.id, status="cancelled",
            result={"cancelled": True, "reason": reason}, is_error=True,
        )
        if done is not None:
            landed.append(tool_call_id)
    return landed


__all__ = [
    "CANCEL_REASON_SUPERSEDED",
    "apply_tool_results",
    "cancel_pending_external",
]
