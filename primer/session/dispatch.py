"""Worker-side session-turn dispatch.

One ``run_one_session_turn`` invocation per claimed session lease.  The
worker pool calls this with the :class:`Lease` it received from the
:class:`ClaimEngine`; the function drives one full execution turn,
persists every :class:`StreamEvent` as a :class:`SessionMessageRecord`
to the workspace's ``messages.jsonl`` via :class:`WorkspaceMessageWriter`,
publishes a ``session:{sid}:tick`` event per record so live WebSocket
subscribers see real-time deltas, honours cancel signals delivered over
the event bus, and handles :class:`YieldToWorker` parks.

Return value:
  A :class:`ReleaseOutcome` the caller passes to
  ``engine.release(lease, outcome=...)``:
  - Normal completion: ``ReleaseOutcome(success=True, drop_lease=True)``
  - Parked (YieldToWorker): ``ReleaseOutcome(success=True, drop_lease=False)``
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from primer.int.claim import ClaimKind, Lease, ReleaseOutcome
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
from primer.model.workspace_session import (
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import YieldToWorker
from primer.session.persistence import (
    WorkspaceIO,
    WorkspaceMessageWriter,
    _CoalesceState,
    translate_stream_event,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class SessionDispatchDeps:
    """Bundle of runtime dependencies the worker injects per session task."""

    storage_provider: StorageProvider
    workspace_io: WorkspaceIO
    event_bus: EventBus

    # Callable that receives a WorkspaceSession row and returns an executor
    # whose ``invoke(messages)`` is an async generator of StreamEvents.
    # Type: Callable[[WorkspaceSession], Awaitable[Any]]
    build_executor: Callable[[WorkspaceSession], Awaitable[Any]]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_one_session_turn(
    lease: Lease,
    deps: SessionDispatchDeps,
) -> ReleaseOutcome:
    """Drive a single session turn; persist records; honour cancel/yield.

    Args:
        lease: The claim lease (``kind=ClaimKind.SESSION``).
        deps:  Runtime dependencies bundle.

    Returns:
        :class:`ReleaseOutcome` for the caller to pass to
        ``engine.release(lease, outcome=...)``.
    """
    assert lease.kind == ClaimKind.SESSION, (
        f"run_one_session_turn called with wrong kind: {lease.kind!r}"
    )
    session_id = lease.entity_id

    # ------------------------------------------------------------------
    # 1. Load session row
    # ------------------------------------------------------------------
    session_storage = deps.storage_provider.get_storage(WorkspaceSession)
    session = await session_storage.get(session_id)
    if session is None:
        logger.warning("session %s vanished before dispatch", session_id)
        return ReleaseOutcome(success=False, drop_lease=True)

    # Early-exit checks that don't need an executor:
    # * If the row is already ENDED (lease leaked through somehow) just
    #   drop the lease; nothing to do.
    # * If cancel_requested is set on the row — set by REST cancel before
    #   any worker observed it, or carried over from a previous process
    #   that died mid-turn — transition to ENDED/cancelled without
    #   running another turn. This is what makes "I cancelled it but
    #   nothing happened" actually terminate after the api restarts.
    if session.status == SessionStatus.ENDED:
        return ReleaseOutcome(success=True, drop_lease=True)
    if session.cancel_requested:
        session.status = SessionStatus.ENDED
        session.ended_reason = "cancelled"
        session.ended_at = _now()
        await session_storage.update(session)
        return ReleaseOutcome(success=True, drop_lease=True)

    # ------------------------------------------------------------------
    # 2. Build executor
    # ------------------------------------------------------------------
    executor = await deps.build_executor(session)
    if executor is None:
        logger.warning("executor builder returned None for session %s", session_id)
        return ReleaseOutcome(success=False, drop_lease=True)

    # ------------------------------------------------------------------
    # 3. Open WorkspaceMessageWriter + cancel-watcher
    # ------------------------------------------------------------------
    writer = WorkspaceMessageWriter(
        workspace_io=deps.workspace_io,
        session_id=session_id,
    )
    cancel_requested = False
    cancel_reason: str = "operator_interrupt"

    cancel_event = asyncio.Event()
    cancel_task = asyncio.create_task(
        _cancel_watcher(deps.event_bus, session_id, cancel_event),
        name=f"sess-cancel-{session_id}",
    )

    # ------------------------------------------------------------------
    # 4. Stream events from executor
    # ------------------------------------------------------------------
    coalesce_state = _CoalesceState()

    try:
        async for event in executor.invoke([]):
            # Translate StreamEvent → SessionMessageRecord(s)
            result = translate_stream_event(event, coalesce_state)
            if result is None:
                # Check cancel between events even when nothing was produced
                if cancel_event.is_set():
                    cancel_requested = True
                    break
                continue

            # Normalise to list
            records: list[SessionMessageRecord]
            if isinstance(result, list):
                records = result
            else:
                records = [result]

            for rec in records:
                seq = await writer.append(rec)
                await deps.event_bus.publish(
                    f"session:{session_id}:tick", {"seq": seq}
                )

            # Honour cancel after processing the current batch
            if cancel_event.is_set():
                cancel_requested = True
                break

    except YieldToWorker as park:
        # ------------------------------------------------------------------
        # 5a. Parked turn — write YIELDED record, flush, publish tick, park
        # ------------------------------------------------------------------
        rec = _yielded_record(park)
        seq = await writer.append(rec)
        await writer.flush()
        await deps.event_bus.publish(
            f"session:{session_id}:tick", {"seq": seq}
        )
        return ReleaseOutcome(success=True, drop_lease=False)

    except Exception:
        logger.exception(
            "session %s executor raised unexpected error; releasing claim",
            session_id,
        )
        error_rec = SessionMessageRecord(
            seq=1,
            kind=SessionMessageKind.ERROR,
            payload={"message": "unexpected executor error", "code": "executor_error"},
            created_at=_now(),
        )
        seq = await writer.append(error_rec)
        await writer.flush()
        await deps.event_bus.publish(
            f"session:{session_id}:tick", {"seq": seq}
        )
        return ReleaseOutcome(success=False, drop_lease=True)

    finally:
        cancel_task.cancel()
        try:
            await cancel_task
        except (asyncio.CancelledError, Exception):
            pass

    # ------------------------------------------------------------------
    # 5b. Cancel path — write CANCELLED record
    # ------------------------------------------------------------------
    if cancel_requested:
        rec = _cancelled_record(cancel_reason)
        seq = await writer.append(rec)
        await writer.flush()
        await deps.event_bus.publish(
            f"session:{session_id}:tick", {"seq": seq}
        )
        return ReleaseOutcome(success=True, drop_lease=True)

    # ------------------------------------------------------------------
    # 6. Clean completion — write DONE record (if not already written by
    #    translate_stream_event), flush, final tick
    # ------------------------------------------------------------------
    # flush any remaining buffer (translate_stream_event already emitted
    # the DONE record if the executor sent a Done event; no extra record needed)
    await writer.flush()

    return ReleaseOutcome(success=True, drop_lease=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _yielded_record(park: YieldToWorker) -> SessionMessageRecord:
    """Build a YIELDED SessionMessageRecord from a YieldToWorker exception."""
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.YIELDED,
        payload={
            "event_key": park.yielded.event_key,
            "tool_name": park.yielded.tool_name,
            "tool_call_id": park.tool_call_id,
        },
        created_at=_now(),
    )


def _cancelled_record(reason: str) -> SessionMessageRecord:
    """Build a CANCELLED SessionMessageRecord."""
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.CANCELLED,
        payload={"reason": reason},
        created_at=_now(),
    )


async def _cancel_watcher(
    event_bus: EventBus,
    session_id: str,
    cancel_event: asyncio.Event,
) -> None:
    """Subscribe to the event bus and set cancel_event when cancel fires."""
    sub = event_bus.subscribe()
    try:
        async for event in sub:
            if event.event_key == f"session:{session_id}:cancel":
                cancel_event.set()
                return
    except asyncio.CancelledError:
        return
    finally:
        await sub.aclose()


__all__ = ["SessionDispatchDeps", "run_one_session_turn"]
