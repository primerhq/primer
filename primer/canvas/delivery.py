"""Delivering a user's canvas interaction to the agent, by the state of its session (pivot P5, Task 12; plan Decisions 7 and 9).

``POST .../canvas/events`` records an interaction (``CanvasService.record_event``: the canvas row, the ``canvas.event`` platform event,
the tap frame) and then hands it here. What happens next depends on the session, read fresh:

* ENDED (or gone): ``recorded``. The route refuses an ended session before it records anything; this covers the one that ended between.
* parked on a ``canvas_wait`` the event matches (``primer.canvas.wait.canvas_wait_matches``, the rule the wait's subscription filter is
  built from): ``wait``. The event dispatcher resumes the wait from the ``canvas.event``; here only its ``canvas_event`` record is written.
  Only a park that is still ``parked`` takes it: a wait already being resumed has its answer, so a second click is queued behind its turn.
* a silent event (a Select change, an audio event, a link open, an Html ``set_value``): ``recorded``, with a ``canvas_event`` record
  (skipped while a turn runs: plan Decision 2). It reaches the agent in the next digest, or in ``canvas_get``.
* a waking event while a turn is open, or while the session is parked on anything else (an ``ask_user``, an approval, a wait on other
  elements): ``queued``, as a steer whose text is the ``<canvas_event>`` block (``store_pending_steer``). The park is untouched, so a
  click never answers a gate (Review Focus 1); the drain realizes it at the next turn boundary.
* else: ``woken``, the block as the session's next instruction (``wake_session``, ``human_intent=True``: a click is a person). A paused
  session is resumed, as a person's typed message resumes it. A wake the session refuses (gone, or ended since) is ``recorded``.

**What the user entered never leaves the session (the lead's ruling 1 on S5, the rule of #872).** A click's ``value`` and an Html emit's
``data`` are in the TEXT the session's own agent reads, the ``<canvas_event>`` block and the ``<canvas_changes>`` digest
(``primer.canvas.blocks``), and nowhere else: every structured copy is the event without those two keys, removed and not masked, through
``primer.events.visibility.payload_without_input``: the queued steer's ``attribution`` and the USER_INPUT's ``payload.canvas_event``
(:func:`event_attribution`, served by ``GET /v1/sessions/{id}`` and the transcript to any user), the digest's payload, and the
``canvas_event`` record (:func:`event_record`).

**Read marks (audit G8-07; the lead's ruling on #795).** ``wake_session`` marks read the events it hands the agent and no other: the click
it delivers (its seq rides ``payload.canvas_event``, so a realized queued click is marked when it is realized) and the digest's lines. An
event the digest leaves out (past :data:`~primer.canvas.blocks.MAX_DIGEST_LINES`, or queued to arrive as its own block) stays unread.

Session core reaches this module lazily (``primer.session.enqueue`` imports it inside functions), and a canvas failure never fails a wake.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from primer.canvas.blocks import MAX_DIGEST_LINES, canvas_changes_block, canvas_event_block
from primer.canvas.records import append_canvas_record
from primer.canvas.service import CanvasService
from primer.canvas.state import CanvasState
from primer.canvas.wait import CANVAS_WAIT_TOOL, canvas_wait_matches, queued_canvas_event_ids
from primer.events.visibility import payload_without_input
from primer.model.canvas import CanvasEvent, SessionCanvas
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import (
    CANVAS_DIGEST_PAYLOAD_KEY,
    CANVAS_EVENT_PAYLOAD_KEY,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.enqueue import SessionWakeDeps, SessionWakeReceipt, wake_session
from primer.session.pending_gates import enumerate_pending_gates
from primer.session.pending_messages import store_pending_steer
from primer.session.steer_routing import ROUTE_PENDING, route_steer

logger = logging.getLogger(__name__)

DELIVERY_WAIT = "wait"
DELIVERY_WOKEN = "woken"
DELIVERY_QUEUED = "queued"
DELIVERY_RECORDED = "recorded"

#: The event type whose input keys every structured copy drops (``primer.events.visibility.INPUT_KEYS``).
_EVENT_TYPE = "canvas.event"
#: What an attribution names of an event, always; ``name`` and ``position`` are added when the event has them.
_ATTRIBUTION_KEYS = ("id", "seq", "element", "type", "kind", "label", "by")
_OPTIONAL_KEYS = ("name", "position")


def event_record(event: CanvasEvent) -> dict[str, Any]:
    """The payload of ``event``'s ``canvas_event`` record: the public event without what the user entered (``value``, ``data``)."""
    return payload_without_input(_EVENT_TYPE, event.public())


def event_attribution(event: CanvasEvent) -> dict[str, Any]:
    """What a queued steer and a USER_INPUT record carry of ``event`` under ``canvas_event``: who did what to which element.

    Never the input: the event's ``value`` and ``data`` are only in the ``<canvas_event>`` block's text. ``seq`` is how the wake that
    delivers the event marks it read.
    """
    public = event_record(event)
    attribution = {key: public.get(key) for key in _ATTRIBUTION_KEYS}
    attribution.update({key: public[key] for key in _OPTIONAL_KEYS if public.get(key) is not None})
    return attribution


def _a_wait_takes(row: WorkspaceSession, event: CanvasEvent) -> bool:
    """Whether ``row`` is parked on a ``canvas_wait`` (its own, or a graph node's) that ``event`` ends."""
    if row.parked_status != "parked" or not isinstance(row.parked_state, dict):
        return False
    public = event.public()
    return any(
        entry.get("kind") == CANVAS_WAIT_TOOL and canvas_wait_matches(entry.get("resume_metadata") or {}, public)
        for entry in enumerate_pending_gates(row.parked_state)
    )


async def deliver_canvas_event(
    *,
    event: CanvasEvent,
    state: CanvasState,
    session_id: str,
    workspace_id: str,
    storage_provider: Any,
    scheduler: Any,
    claim_engine: Any,
    workspace_registry: Any,
    event_bus: Any,
) -> str:
    """Deliver the recorded ``event`` to ``session_id`` by the session's state (see the module docstring) and say how.

    ``state`` is the canvas after the event (``CanvasService.state_of``): its user values go into the block. The session row's own
    workspace is the one written to; ``workspace_id`` (the caller's) only names it in the log when the session is gone. Returns one of
    ``wait``, ``woken``, ``queued`` and ``recorded``.
    """
    row = await storage_provider.get_storage(WorkspaceSession).get(session_id)
    if row is None or row.status == SessionStatus.ENDED:
        logger.info("canvas event %s: session %s (workspace %s) is gone or ended; recorded only", event.id, session_id, workspace_id)
        return DELIVERY_RECORDED
    home = row.workspace_id

    async def record() -> None:
        await append_canvas_record(
            storage_provider=storage_provider, workspace_registry=workspace_registry, event_bus=event_bus, session_id=session_id,
            workspace_id=home, kind=SessionMessageKind.CANVAS_EVENT, payload=event_record(event),
        )

    if _a_wait_takes(row, event):
        await record()
        return DELIVERY_WAIT
    if not event.wake:
        await record()
        return DELIVERY_RECORDED
    block = canvas_event_block(event, state=state)
    carried = {CANVAS_EVENT_PAYLOAD_KEY: event_attribution(event)}
    if route_steer(row) == ROUTE_PENDING:
        await store_pending_steer(
            storage_provider=storage_provider, session=row, text=block, workspace_registry=workspace_registry, event_bus=event_bus,
            attribution=carried,
        )
        return DELIVERY_QUEUED
    receipt = SessionWakeReceipt()
    try:
        await wake_session(
            workspace_id=home,
            session_id=session_id,
            instruction=block,
            human_intent=True,
            extra_payload=carried,
            receipt=receipt,
            deps=SessionWakeDeps(
                storage_provider=storage_provider, scheduler=scheduler, claim_engine=claim_engine,
                workspace_registry=workspace_registry, event_bus=event_bus,
            ),
        )
    except (NotFoundError, ConflictError):
        logger.warning("canvas event %s: session %s cannot take it now; recorded only", event.id, session_id, exc_info=True)
        return DELIVERY_RECORDED
    return DELIVERY_QUEUED if receipt.outcome == "queued" else DELIVERY_WOKEN


# ---- the digest (plan Decision 9) ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CanvasDigest:
    """The canvas changes a wake adds to its instruction.

    ``text`` is the ``<canvas_changes>`` block (the second text part), ``payload`` what the USER_INPUT record keeps under
    ``canvas_digest`` (``{"rev", "events": [event_attribution ...]}``: the listed events, never their input) and ``seqs`` the events the
    block lists, which the wake marks read once the instruction is written (and no other: audit G8-07).
    """

    text: str
    payload: dict[str, Any]
    seqs: tuple[int, ...]


async def canvas_digest_for(storage_provider: Any, session_id: str, *, exclude_event_id: str | None) -> CanvasDigest | None:
    """The digest of ``session_id``'s unread canvas events, or ``None`` when it has no canvas or nothing to list.

    Left out: the event ``exclude_event_id`` (the click this wake delivers as its block) and every click still queued as a steer (it
    reaches the agent as its own block when the queue is realized; listed here too it would arrive twice). They stay unread, and so do
    the events past :data:`~primer.canvas.blocks.MAX_DIGEST_LINES`: the block counts them and the agent reads them with ``canvas_get``,
    or in the next digest.
    """
    row = await storage_provider.get_storage(SessionCanvas).get(session_id)
    if row is None:
        return None
    unread = [event for event in row.unseen_events() if event.id != exclude_event_id]
    if not unread:
        return None
    queued = await queued_canvas_event_ids(storage_provider, session_id)
    unread = [event for event in unread if event.id not in queued]
    if not unread:
        return None
    listed = unread[:MAX_DIGEST_LINES]
    return CanvasDigest(
        text=canvas_changes_block(unread, rev=row.rev),
        payload={"rev": row.rev, "events": [event_attribution(event) for event in listed]},
        seqs=tuple(event.seq for event in listed),
    )


async def mark_delivered(storage_provider: Any, session_id: str, seqs: Sequence[int]) -> None:
    """Mark the canvas events ``seqs`` read, and no other (``CanvasService.mark_events_seen``): what a wake handed the agent, the
    digest's lines and the click it delivered. An earlier event left out stays unread (audit G8-07)."""
    await CanvasService(storage_provider).mark_events_seen(session_id, seqs)


__all__ = [
    "CANVAS_DIGEST_PAYLOAD_KEY",
    "CANVAS_EVENT_PAYLOAD_KEY",
    "CanvasDigest",
    "DELIVERY_QUEUED",
    "DELIVERY_RECORDED",
    "DELIVERY_WAIT",
    "DELIVERY_WOKEN",
    "canvas_digest_for",
    "deliver_canvas_event",
    "event_attribution",
    "event_record",
    "mark_delivered",
]
