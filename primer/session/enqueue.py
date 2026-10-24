"""Session auto-wake — the keystone that unifies invoke = steer = resume.

An inbound user message re-arms a session's scheduler claim so the worker
runs a fresh turn, regardless of the session's current lifecycle state.
Mirrors the deleted chat surface's persist+wake
tail: the session's on-disk ``messages.jsonl`` IS the FIFO queue
(``AgentSession.append_instruction``); the scheduler-visible
``WorkspaceSession`` row carries the claim state (``turn_status`` +
``ClaimEngine`` lease + scheduler enqueue).

Behaviour by status (studio-agents-interact §4.2 / §5.1):
  * CREATED           -> invoke  (transition to RUNNING; run with the message)
  * RUNNING / WAITING -> steer   (queue as the next turn; re-arm the claim)
  * PAUSED, human_intent=True  -> resume (clear pause; transition to RUNNING)
  * PAUSED, human_intent=False -> hold   (queue the message; pause untouched
                                          -- 01a08c08, see ``human_intent``)
  * ENDED             -> restart (reopen + invocation divider, then invoke);
                         a message no person wrote (a peer's, a trigger's,
                         a tool's) does not restart a CANCELLED session
                         (OnlyAPersonReopens)

Uniform "send a message" semantics: every clean agent turn ends the session,
and sending a NEW message to an ENDED session reopens it (reopen the on-disk
slot, write an invocation divider, run a fresh turn) instead of erroring. A
non-restartable ``ended_reason`` (``workspace_lost``/``force_deleted``) still
raises ConflictError.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from primer.int.claim import ClaimKind
from primer.model.chat import TextPart
from primer.model.except_ import ConflictError, NotFoundError, PrimerError
from primer.model.workspace_session import (
    CANVAS_DIGEST_PAYLOAD_KEY,
    CANVAS_EVENT_PAYLOAD_KEY,
    MACHINE_ATTRIBUTION_KEY,
    PEER_PAYLOAD_KEY,
    PeerAttribution,
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.mutation_lock import session_lifecycle_lock
from primer.session.persistence import WorkspaceMessageWriter, WorkspaceWriteTimeout
from primer.session.reset import OnlyAPersonReopens, _reopen_ended_locked, no_turn_holds, refuse_a_reopen_it_may_not_make
from primer.session.seq_reservation import reserve_next_seq
from primer.session.title import derive_title_from_text
from primer.storage import raw_generation

logger = logging.getLogger(__name__)

_RESUMABLE = {
    SessionStatus.CREATED,
    SessionStatus.PAUSED,
    SessionStatus.WAITING,
}


def queued_attribution(peer: PeerAttribution | None, *, by_a_person: bool) -> dict | None:
    """What a queued message carries about its sender, so the drain that realizes it later wakes with the right rule.

    A message from another session carries its ``peer`` attribution (the hop count survives the queue); one no person wrote and no session sent (a
    trigger fire, a tool call with no calling session) carries the machine marker (``{"machine": True}``), so it does not reopen a session an
    operator cancelled when it is realized (lead ruling 1 of the G7 fixes); a person's message carries nothing, as it always did.
    """
    if peer is not None:
        return peer.as_attribution()
    if not by_a_person:
        return {MACHINE_ATTRIBUTION_KEY: True}
    return None


def _delivered_canvas_event(extra_payload: dict | None) -> dict | None:
    """The canvas click a wake delivers (pivot P5): ``extra_payload["canvas_event"]``, the event without what the user entered."""
    found = extra_payload.get(CANVAS_EVENT_PAYLOAD_KEY) if isinstance(extra_payload, dict) else None
    return found if isinstance(found, dict) else None


def _attribution_to_queue(peer: PeerAttribution | None, *, by_a_person: bool, extra_payload: dict | None) -> dict | None:
    """What a routed message this wake queues instead of writing carries (:func:`queued_attribution`), plus the canvas click it delivers, so
    the drain that realizes it later hands the click back (``realize_next_pending``) and marks it read then, not now."""
    attribution = queued_attribution(peer, by_a_person=by_a_person)
    delivered = _delivered_canvas_event(extra_payload)
    if delivered is None:
        return attribution
    return {**(attribution or {}), CANVAS_EVENT_PAYLOAD_KEY: delivered}


def _queueable(extra_payload: dict | None) -> bool:
    """Whether a queued message can carry ``extra_payload``: none, or only the canvas click (its attribution holds it)."""
    return not extra_payload or set(extra_payload) == {CANVAS_EVENT_PAYLOAD_KEY}


#: How long a wake waits on the canvas (its digest read, its read marks) before it goes on without them (the lead's ruling 6 on #892 round
#: 2): the wake holds the session's turn while it waits, so a canvas table that does not answer must not hold the session with it.
_CANVAS_TIMEOUT_S = 2.0


async def _canvas_digest(deps: "SessionWakeDeps", session_id: str, extra_payload: dict | None) -> Any:
    """The canvas changes the agent has not read, to ride the instruction this wake writes (pivot P5, plan Decision 9), or ``None``.

    The click this wake delivers is left out (it is the instruction). Session core reaches the canvas lazily, and a canvas never fails a
    wake: a failure, or no answer within :data:`_CANVAS_TIMEOUT_S`, is logged once and the wake goes on without a digest.
    """
    try:
        from primer.canvas.delivery import canvas_digest_for

        delivered = _delivered_canvas_event(extra_payload)
        exclude = delivered.get("id") if delivered is not None else None
        async with asyncio.timeout(_CANVAS_TIMEOUT_S):
            return await canvas_digest_for(
                deps.storage_provider, session_id, exclude_event_id=exclude if isinstance(exclude, str) else None,
            )
    except TimeoutError:
        logger.warning("canvas digest skipped for %s: the canvas did not answer within %s s", session_id, _CANVAS_TIMEOUT_S)
        return None
    except Exception:  # noqa: BLE001 -- the canvas is advisory to a wake
        logger.warning("canvas digest skipped for %s", session_id, exc_info=True)
        return None


async def _mark_canvas_read(deps: "SessionWakeDeps", session_id: str, digest: Any, extra_payload: dict | None) -> None:
    """Mark read the canvas events this wake handed the agent, and no other (audit G8-07): the digest's lines and the click it delivers
    (the lead's ruling on #795, so a later ``canvas_get`` or ``canvas_wait`` does not return it again). Best effort: an event left unread
    is listed again by the next digest, which is the safe way to fail."""
    seqs = list(digest.seqs) if digest is not None else []
    delivered = _delivered_canvas_event(extra_payload)
    seq = delivered.get("seq") if delivered is not None else None
    if isinstance(seq, int) and not isinstance(seq, bool):
        seqs.append(seq)
    if not seqs:
        return
    try:
        from primer.canvas.delivery import mark_delivered

        async with asyncio.timeout(_CANVAS_TIMEOUT_S):
            await mark_delivered(deps.storage_provider, session_id, seqs)
    except TimeoutError:
        logger.warning(
            "canvas events %s of session %s stayed unread: the canvas did not answer within %s s", seqs, session_id, _CANVAS_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 -- the instruction is written; the marks are advisory
        logger.warning("canvas events %s of session %s stayed unread", seqs, session_id, exc_info=True)


class CanvasClickDoesNotReopen(ConflictError):
    """A canvas click (``extra_payload["canvas_event"]``) found its session ENDED: a click never reopens an ended or cancelled session
    (design d.8; the lead's ruling 4 on #892 round 2, over ticket 01a1120e's rule for a person's typed steer). Raised before anything is
    written. The delivery answers ``recorded``; the drain drops a queued click with a summary record, and the click stays unread."""


@dataclass
class SessionWakeDeps:
    """Collaborators :func:`wake_session` needs.

    ``event_bus`` is optional: when present a ``session:{sid}:claimable``
    event is published for observability (the scheduler enqueue + claim
    upsert are the load-bearing pulse). ``workspace_registry`` is required
    when an ``instruction`` is supplied (to reach the on-disk slot's FIFO).
    """

    storage_provider: Any
    scheduler: Any
    claim_engine: Any
    workspace_registry: Any
    event_bus: Any | None = None


@dataclass
class SessionWakeReceipt:
    """How a routed wake handled its instruction after taking the lock."""

    outcome: Literal["woken", "queued", "skipped_busy", "gone"] = "woken"


async def _reopen_a_stale_ended_slot(slot: Any) -> None:
    """The row says the session is alive (this branch is for a row that is not ENDED), so a slot that says ENDED is a stale mirror: reopen it.

    A turn that FAILED leaves the executor's on-disk slot ENDED, and a session that rests after it (C-024) takes this in-place path, not the
    ENDED branch that reopens the slot. The worker reopens the slot it holds at the failure exit; this covers the handle THIS process holds
    (a cached one never rewinds an ENDED view it adopted from disk) and a slot whose reopen there did not land. ``reopen`` is a no-op on a slot
    that is not ENDED. A slot without ``status`` (a test double) is left alone.
    """
    status = getattr(slot, "status", None)
    reopen = getattr(slot, "reopen", None)
    if status is not None and reopen is not None and await status() == SessionStatus.ENDED:
        await reopen()


async def _workspace_is_gone(workspace_registry: Any, workspace_id: str) -> bool:
    """Whether *workspace_id* has been, or is being, destroyed.

    Used after a workspace write failed, to tell the destroy undoing the write from a fault of the write. True when this process is destroying the
    workspace right now (``is_being_destroyed``: the runtime goes first and the row last, so the row alone cannot say so in between), when its row is
    gone, or when the registry can hand out no live instance for it. False when it can say none of that, and when a check cannot be made at all: a
    check that itself fails is no evidence that the workspace is gone, so the original failure stands.
    """
    if workspace_registry is None:
        return False
    being_destroyed = getattr(workspace_registry, "is_being_destroyed", None)
    if callable(being_destroyed) and being_destroyed(workspace_id):
        return True
    for lookup in ("get_workspace_row", "get_workspace"):
        look = getattr(workspace_registry, lookup, None)
        if look is None:
            continue
        try:
            await look(workspace_id)
        except NotFoundError:
            return True
        except Exception:  # noqa: BLE001 -- see above: a failed check proves nothing
            logger.warning(
                "wake_session: could not tell whether workspace %s is gone (%s)", workspace_id, lookup, exc_info=True,
            )
            return False
    return False


@asynccontextmanager
async def _workspace_write(
    deps: "SessionWakeDeps", *, workspace_id: str, what: str,
) -> AsyncIterator[None]:
    """Run the workspace-side writes of a wake, and turn a failure that is the workspace going away under them into a NotFoundError (404).

    ``WorkspaceRegistry.destroy`` tears the runtime down (the cached instance goes, the sessions on it end, the directory is removed), ends the
    sessions' rows and only then deletes the workspace row, and none of that is serialised against a caller that already holds the live workspace. A
    delete that lands during a wake therefore fails the writes that follow (e2e ``test_t0716_workspace_delete_racing_steer_clean_envelopes``):

    * a filesystem error (``OSError``) is reported as the removal it nearly always is, as it always was;
    * the local state repository's other failure, a git command that exits non-zero in a directory that is gone (a ``RuntimeError``), is reported as one
      only when the registry says the workspace is gone or going (:func:`_workspace_is_gone`); with the workspace alive it is a real fault and keeps its
      type and its traceback;
    * a ``PrimerError`` already carries its own envelope (an ENDED slot refuses an instruction with a ConflictError, 409) and passes through, and so
      does the writer's timeout (503: the workspace is there and slow, which is not a removal).

    The original failure stays on ``__cause__`` for the log.
    """
    try:
        yield
    except WorkspaceWriteTimeout:
        # A TimeoutError is an OSError: without this the writer's "the workspace did not accept the write" would be reported as "the workspace was removed"
        # (404). It is a 503 (WorkspaceUnreachableError) and passes through.
        raise
    except PrimerError:
        raise
    except OSError as exc:
        raise NotFoundError(f"workspace {workspace_id!r} was removed while {what}") from exc
    except Exception as exc:
        if not await _workspace_is_gone(deps.workspace_registry, workspace_id):
            raise
        logger.info(
            "wake_session: workspace %s was removed while %s; answering not-found (%s: %s)",
            workspace_id, what, type(exc).__name__, exc,
        )
        raise NotFoundError(f"workspace {workspace_id!r} was removed while {what}") from exc


async def wake_session(
    *,
    workspace_id: str,
    session_id: str,
    instruction: str | None,
    deps: SessionWakeDeps,
    human_intent: bool,
    external_tools: "list | None" = None,
    extra_parts: "list | None" = None,
    extra_payload: "dict | None" = None,
    peer: PeerAttribution | None = None,
    open_turn: Literal["queue", "skip"] = "queue",
    receipt: SessionWakeReceipt | None = None,
    by_a_person: bool | None = None,
    queued_message_id: str | None = None,
) -> WorkspaceSession:
    """Append ``instruction`` (if any) and re-arm the session's claim.

    ``extra_parts`` (vision/document attachments, already resolved to
    artifact-backed :class:`~primer.model.chat.Part` objects) rides the
    same :class:`~primer.model.chat.Message` the instruction's
    :class:`~primer.model.chat.TextPart` does -- forwarded to
    :meth:`AgentSession.append_instruction`. ``extra_payload`` merges into
    the USER_INPUT display record's ``payload`` dict (e.g. attachment
    paths) so the transcript surface can show what was attached without
    reconstructing it from the full Message. Both ``None`` (every
    existing caller) is unchanged behaviour.

    ``peer`` (pivot P3) says the instruction was sent by another session
    (``message_session``). It is recorded on the USER_INPUT record
    (``payload["peer"]``), sets ``WorkspaceSession.peer_hops`` to the
    message's hop count in the same row write (every other wake that lands
    an instruction resets it to 0, since a person, a trigger or a channel
    starts a fresh chain), and rides into the queued message when the pause
    hold below stores one. A wake that lands no instruction leaves the hop
    count alone.

    The canvas (pivot P5): an instruction this wake writes carries the
    canvas changes the agent has not read as one more text part (the
    ``<canvas_changes>`` digest, ``primer.canvas.delivery``), and the
    USER_INPUT record keeps them as ``payload["canvas_digest"]``.
    ``extra_payload["canvas_event"]`` says the instruction IS a canvas click
    (its ``<canvas_event>`` block); it is left out of the digest, and a
    routed wake may still queue it (the queued message's attribution holds
    it). Once the instruction is written the wake marks read the digest's
    events and that click, and no other (audit G8-07); a message this wake
    queues instead keeps the click, to be marked when it is realized.
    Neither the digest nor the marks ever fail the wake: a canvas that
    cannot be read or written is logged and skipped.

    ``human_intent`` (01a08c08, REQUIRED, no default on purpose): does a
    live human's decision to re-engage stand behind THIS wake? True for
    the console steer endpoint and a channel thread reply. False for a
    trigger's session_append subscription, an agent-to-agent steer tool
    call, and a queued message's own later realization (a replay of an
    EARLIER human action, not fresh intent). A pause is a control a human
    exercised on purpose; wake_session must not be the place that quietly
    decides an unrelated automated event counts as that human changing
    their mind. See the pause handling below -- a non-human wake of a
    session paused on purpose (``pause_requested``, or status PAUSED with
    no flag, which is how the pause route pauses an idle session) queues
    instead of clearing the pause, so a new caller literally cannot
    compile without someone consciously stating which side of that line
    they're on.

    ``by_a_person`` says whether a person WROTE the instruction, which is not
    always whether a person is acting now: the drain realizing a person's
    steer that was queued earlier passes ``human_intent=False`` (the replay
    must not clear a pause that landed since) and ``by_a_person=True``. It
    decides whether the wake may reopen a session an operator cancelled, and
    it travels into the queue with a message this wake stores
    (:func:`queued_attribution`). ``None`` (every direct caller) means
    ``human_intent``.

    Raises NotFoundError (missing / workspace mismatch). An ENDED session is
    reopened in place (reopen slot + invocation divider + ENDED->CREATED)
    before the normal invoke flow, so a new message restarts it; a
    non-restartable ``ended_reason`` (``workspace_lost``/``force_deleted``)
    still raises ConflictError. A message no person wrote (``by_a_person``
    False: a message from another session, a trigger fire, a tool call, and
    any of them realized from the queue later) does not restart a session
    that ended as ``cancelled``: only a person's message does, so that one
    raises :class:`~primer.session.reset.OnlyAPersonReopens` (a ConflictError)
    before anything is written. Taken under the session lifecycle lock, which
    serialises a wake against a cancel, pause or resume of THIS process only:
    every decision below is also fenced in the data, so it holds across
    processes (the API pod and the worker pods).

    The pause hold below stores a held message through ``store_pending_steer``,
    which refuses a message from another session when the queue is full: that
    raises ``PendingQueueFull`` (a ConflictError) out of this function, before
    anything is written, and the pause stays as it was.

    One turn per wake (audit G7-03). A routed caller passes ``receipt``
    (``deliver_steer``, the REST steer route, the drain's realize). For a
    plain text instruction the wake then decides the open turn on the row
    read under the lock: a turn that is armed, running or parked, or older
    messages still waiting in the pending queue, defer the message (queued, or
    ``skipped_busy`` when ``open_turn="skip"``); and an idle turn is armed by
    ONE conditional write of the row (``turn_status`` idle -> claimable,
    fenced on the ``last_seq``, status and pause flag it read, which also
    reserves the USER_INPUT's seq) BEFORE the USER_INPUT is written. Of two
    wakes of one idle session in any two processes exactly one write lands;
    the other reads the row again, finds the turn armed and defers its message.
    The receipt reports that decision. The USER_INPUT is written AT the
    reserved seq, never seeded from a row read earlier. A wake without a
    receipt, or one carrying per-turn tools or attachment parts (which the
    queue cannot hold), still writes its USER_INPUT whatever the turn's state,
    as it always did, at a seq reserved the same way.

    ``queued_message_id`` is the drain realizing the queue head (the
    instruction IS that queued message): where another wake would be queued,
    it is left where it is (the receipt says ``queued``), nothing is stored
    again, and the receipt says ``gone`` when another drain delivered it
    since it was read. The caller deletes the queued row only when the
    receipt says ``woken``.

    The closing write of the wake is ONE ``patch_if`` of the fields it owns,
    fenced on the status it decided from: a Cancel or a pause that landed on
    another process since wins, and the session is not woken.
    """
    sent_by_a_person = human_intent if by_a_person is None else by_a_person
    sessions = deps.storage_provider.get_storage(WorkspaceSession)
    # A plain text instruction from a routed caller can wait in the pending queue, so the open turn is decided for it here (see the docstring).
    # Calls carrying per-turn tools or attachment parts stay on their existing path: PendingSessionMessage cannot represent those payloads. A
    # canvas click (pivot P5) can: its attribution holds the event.
    routed = (
        receipt is not None
        and bool(instruction)
        and external_tools is None
        and not extra_parts
        and _queueable(extra_payload)
    )
    # A wake that reopens an ENDED session starts its next invocation with this message, whatever an earlier invocation left queued (the turn
    # boundary after it realizes that): an ended session is always woken (route_steer).
    reopened = False
    async with session_lifecycle_lock().acquire(session_id):
        # ``armed`` is the row as this wake's own conditional write left it once the wake holds the turn as ``arming`` (its reopen, or its
        # arm); its ``armed_at`` fences the promotion and the give-back. ``settled`` says what became of that turn: ``promoted`` to
        # claimable, given to a ``pause`` or a Cancel that won, or ``taken`` by another writer. A wake that holds the turn and leaves by any
        # other way (an early return, an exception, a cancellation) settles it in the ``finally`` below: no path leaves the row arming
        # with nothing to run, or a written message unclaimed (audit G7-03, review N1).
        armed: WorkspaceSession | None = None
        settled: str | None = None
        message_written = False
        try:
            for _attempt in range(_ARM_ATTEMPTS):
                row = await sessions.get(session_id)
                if row is None or row.workspace_id != workspace_id:
                    raise NotFoundError(
                        f"Session {session_id!r} does not exist on workspace "
                        f"{workspace_id!r}"
                    )
                if queued_message_id is not None and not reopened and not await _still_queued(deps.storage_provider, queued_message_id):
                    # Another drain (a turn's checkpoint, the queue sweep) delivered this message after it was read: never twice.
                    if receipt is not None:
                        receipt.outcome = "gone"
                    return row
                if row.status == SessionStatus.ENDED:
                    if _delivered_canvas_event(extra_payload) is not None:
                        # A canvas click is not a reopener (design d.8, the lead's ruling 4 on #892 round 2): it is decided here, on the row read
                        # under the lock, so a Cancel that landed after the delivery read the row, or after the click was queued, still wins.
                        raise CanvasClickDoesNotReopen(f"Session {session_id!r} has ended; a canvas click does not reopen it")
                    # An operator's Cancel sticks (lead ruling R4, and ruling 1 of the G7 fixes): a message no person wrote does not undo it.
                    # ``_reopen_ended_locked`` decides it first, here under the lifecycle lock and on the row just read, so a Cancel that landed after
                    # any earlier look is seen; a person's message (``by_a_person``) still reopens.
                    # Reopen the ended session in place so a NEW message restarts it
                    # (reopen slot + invocation divider + ENDED->CREATED). We hold the
                    # non-reentrant session_lifecycle_lock, and reset_session acquires
                    # the SAME lock -- calling it here would deadlock -- so we share
                    # its lock-free reopen body via _reopen_ended_locked. The helper
                    # raises ConflictError for non-restartable ended_reasons. After
                    # this, row is CREATED and the normal wake flow below appends the
                    # USER_INPUT (after the divider) and runs the turn.
                    #
                    # The guard that reports a failed write as "the workspace was
                    # removed" wraps only the writes INTO the workspace (the slot reopen
                    # and the divider), which the helper takes as a factory. It must not
                    # wrap the helper whole: that also calls the database (the
                    # workspace_lost recheck, the workspace lookup, the row update), and
                    # a refused pool connection is an OSError, so it was reported as the
                    # removal of a workspace nobody had touched.
                    #
                    # The reopen is fenced in the data (audit G7-03): it decides with ONE conditional write that also reserves the divider's seq and
                    # marks the turn ``arming`` (this wake's until it promotes it), so two reopens on two processes cannot both land. A row another
                    # wake is arming is busy; a ``running`` flag is a turn still finishing its terminal block unless no turn holds the session (a
                    # flag left behind; the drain realizing the queue head is the boundary itself). A refused fence is decided again on a fresh read.
                    await refuse_a_reopen_it_may_not_make(
                        row, workspace_id=workspace_id, session_id=session_id, workspace_registry=deps.workspace_registry,
                        by_a_person=sent_by_a_person,
                    )
                    if row.turn_status == "arming" or (
                        row.turn_status == "running" and queued_message_id is None and not await no_turn_holds(deps.claim_engine, session_id)
                    ):
                        if routed:
                            return await _defer_routed(
                                deps, row=row, instruction=instruction, peer=peer, by_a_person=sent_by_a_person, open_turn=open_turn,
                                receipt=receipt, queued_message_id=queued_message_id, extra_payload=extra_payload,
                            )
                        continue
                    done = await _reopen_ended_locked(
                        row=row,
                        workspace_id=workspace_id,
                        session_id=session_id,
                        sessions=sessions,
                        workspace_registry=deps.workspace_registry,
                        by_a_person=sent_by_a_person,
                        workspace_write=lambda: _workspace_write(
                            deps, workspace_id=workspace_id, what=f"reopening session {session_id!r}",
                        ),
                        keep_arming=True,
                        allow_running=row.turn_status == "running",
                    )
                    if done is None:
                        continue
                    row, _invocation = done
                    reopened = True
                    armed = row
                    # Surface the invocation divider live before the USER_INPUT tick
                    # below (mirrors reset_session's divider tick). Advisory.
                    if deps.event_bus is not None:
                        try:
                            await deps.event_bus.publish(
                                f"session:{session_id}:tick", {"seq": row.last_seq}
                            )
                        except Exception:  # noqa: BLE001 -- advisory
                            logger.exception(
                                "wake_session: failed to publish divider tick for %s",
                                session_id,
                            )

                if (row.pause_requested or row.status == SessionStatus.PAUSED) and not human_intent:
                    # 01a08c08: the operator paused this session on purpose; a
                    # non-human wake (trigger fire, agent-to-agent steer, a
                    # queued message replaying) must not be the thing that
                    # silently un-pauses it. The pause is on purpose whether it
                    # landed while a turn ran (pause_requested: the worker honours
                    # the flag at the next turn boundary and writes PAUSED, the flag
                    # still set) or while the session was idle (the pause route
                    # writes PAUSED straight onto a CREATED or WAITING row and sets
                    # no flag), so either one holds the session.
                    # Queue the instruction through the
                    # SAME pending-message mechanism deliver_steer already uses
                    # for a busy session (store_pending_steer / realize_next_
                    # pending), rather than forcing a resume nobody asked for or
                    # dropping the message outright. No wake write happens here
                    # -- pause_requested, status, and turn_status all stay exactly
                    # as they were; only last_seq moves, by the seq the
                    # PAUSE_SUPERSEDED record below reserves and, when the queue is
                    # full, by the seq store_pending_steer reserves for the record
                    # that announces the dropped message (ticket 01a11cd8).
                    if queued_message_id is not None:
                        # The drain realizing a message that is already queued: it stays where it is (its place is its order) and nothing is
                        # written; it is realized at a turn boundary after the pause is lifted.
                        if receipt is not None:
                            receipt.outcome = "queued"
                        return row
                    pending_id = None
                    if instruction:
                        # Deferred: pending_messages.py imports wake_session from
                        # this module, so a top-level import here would cycle.
                        from primer.session.pending_messages import store_pending_steer

                        pending = await store_pending_steer(
                            storage_provider=deps.storage_provider,
                            session=row,
                            text=instruction,
                            workspace_registry=deps.workspace_registry,
                            event_bus=deps.event_bus,
                            # The sender and hop count ride the queue (pivot P3): realize_next_pending hands them back to the wake that
                            # delivers this message, or a reply chain would restart at hop 1 each time a pause held it. So does whether a person
                            # wrote it (G7 ruling 1): a held trigger message must not reopen a session cancelled before it is realized. And so
                            # does the canvas click it is (pivot P5; the lead's ruling 8 on #892 round 2), marked read only when it is realized.
                            attribution=_attribution_to_queue(peer, by_a_person=sent_by_a_person, extra_payload=extra_payload),
                        )
                        pending_id = pending.id
                    await _record_pause_superseded(
                        deps, workspace_id=workspace_id, session_id=session_id,
                        row=row, action="queued", pending_id=pending_id,
                    )
                    if receipt is not None:
                        receipt.outcome = "queued"
                    return row

                if routed and not reopened:
                    # The routing read of deliver_steer and of the REST steer path happens before this lock, and the lock is this process's alone.
                    # Decide again on the row just read: a turn that is armed, running or parked, or older messages still waiting in the queue (a
                    # new message never overtakes one: audit G7-03 (b)), defer this one. The drain realizing the queue head is that head, so it
                    # does not wait behind itself; nor does a message that just reopened an ended session (see ``reopened``).
                    from primer.session.pending_messages import has_queued_messages
                    from primer.session.steer_routing import ROUTE_PENDING, route_steer

                    queued = queued_message_id is None and not reopened and await has_queued_messages(deps.storage_provider, session_id)
                    if route_steer(row, queued=queued) == ROUTE_PENDING:
                        return await _defer_routed(
                            deps, row=row, instruction=instruction, peer=peer, by_a_person=sent_by_a_person, open_turn=open_turn,
                            receipt=receipt, queued_message_id=queued_message_id, extra_payload=extra_payload,
                        )

                if not instruction:
                    decided = row
                    break
                # ONE conditional write decides the turn and reserves the USER_INPUT's seq (audit G7-03 (a) and (c)). It is fenced on the row just
                # read (its last_seq, status and pause flag) and, for a routed wake, on the turn being idle and the session not parked; it flips the
                # turn claimable in the same statement. Of two wakes of one idle session, in any two processes, exactly one write lands: the other
                # is refused, reads the row again above, finds the turn armed and defers its message. A refusal from any other change (a record
                # that took the next seq, a Cancel, a pause) is decided again the same way. Nothing has been written when it is refused.
                taken = await _arm_or_reserve(sessions, row, arm=routed, owned=reopened)
                if taken is not None:
                    decided, row = row, taken
                    if routed or reopened:
                        armed = taken
                    break
            else:
                # Every attempt was overtaken by another writer. A plain routed message is queued (the next turn boundary, or the queue sweep,
                # delivers it: never lost, never a second USER_INPUT); a call carrying tools or parts cannot be queued, so it is refused. A turn
                # this wake took is given back in the ``finally``.
                fresh = await sessions.get(session_id)
                if fresh is None or fresh.workspace_id != workspace_id:
                    raise NotFoundError(
                        f"Session {session_id!r} does not exist on workspace "
                        f"{workspace_id!r}"
                    )
                if not routed:
                    raise ConflictError(
                        f"Session {session_id!r} kept changing while the message was being delivered; nothing was written, retry"
                    )
                return await _defer_routed(
                    deps, row=fresh, instruction=instruction, peer=peer, by_a_person=sent_by_a_person, open_turn=open_turn,
                    receipt=receipt, queued_message_id=queued_message_id, extra_payload=extra_payload,
                )

            # 1. Append the user message to the on-disk slot FIFO (the queue) and
            #    persist a USER_INPUT record to messages.jsonl so the sent message
            #    shows in the session transcript. The slot FIFO is the worker's
            #    queue; messages.jsonl is the display log: separate surfaces, so
            #    the USER_INPUT record is written even when the slot is absent
            #    (mirrors the chat lane's append_user_message + reset_session's
            #    divider write via WorkspaceMessageWriter). Exactly one record per
            #    supplied instruction, so a steer/invoke never double-records. It is
            #    written AT the seq the conditional write above reserved (never
            #    seeded from a row read earlier: audit G7-03 (c)).
            user_input_seq: int | None = None
            if instruction:
                ws = await deps.workspace_registry.get_workspace(workspace_id)
                # The canvas changes the agent has not read ride this instruction as one more text part (pivot P5, plan Decision 9), and the
                # record keeps what they were (without what the user entered). The pause hold above writes no instruction, so a held message
                # gets its digest when it is realized.
                digest = await _canvas_digest(deps, session_id, extra_payload)
                if digest is not None:
                    extra_parts = [*(extra_parts or []), TextPart(text=digest.text)]
                    extra_payload = {**(extra_payload or {}), CANVAS_DIGEST_PAYLOAD_KEY: digest.payload}
                # get_workspace() itself is already defended against a
                # concurrently-destroyed workspace (WorkspaceRegistry.get_workspace
                # / the local backend's _reattach both degrade a gone/mid-teardown
                # workspace to a clean NotFoundError). But once a live `ws` handle
                # is in hand, a workspace DELETE racing this same wake_session can
                # still tear down the on-disk root out from under the writes below
                # (WorkspaceRegistry.destroy: pop-from-cache -> aclose -> rmtree,
                # not serialised against a caller already holding `ws`). That is
                # not a real internal error, so _workspace_write maps it to the same
                # NotFoundError this function already raises for "workspace not
                # found" rather than letting it surface as an unhandled 500: a
                # filesystem OSError always, and any other failure (the local state
                # repo's git command failing in a directory that is gone) when the
                # registry says the workspace is gone or going (T0716).
                async with _workspace_write(
                    deps, workspace_id=workspace_id, what=f"writing session {session_id!r}'s instruction",
                ):
                    slot = await ws.get_session(session_id)
                    if slot is not None:
                        await _reopen_a_stale_ended_slot(slot)
                        await slot.append_instruction(
                            instruction, extra_parts=extra_parts,
                        )
                    writer = WorkspaceMessageWriter(
                        workspace_io=ws, session_id=session_id, start_seq=row.last_seq - 1,
                    )
                    user_input_seq = await writer.append(SessionMessageRecord(
                        seq=1,  # overwritten by the writer's monotonic counter: the reserved seq
                        kind=SessionMessageKind.USER_INPUT,
                        payload={
                            "text": instruction,
                            **(extra_payload or {}),
                            **({PEER_PAYLOAD_KEY: peer.model_dump(mode="json")} if peer is not None else {}),
                        },
                        created_at=datetime.now(timezone.utc),
                    ))
                    await writer.flush()
                message_written = True
                if queued_message_id is not None:
                    # The drain's message is in the log now: it leaves the queue BEFORE the turn is promoted, so no turn can run (and drain
                    # the queue) while it is still queued and deliver it a second time (review N4). Were the promotion to fail from here, the
                    # message is in the log and the turn this wake holds is promoted or healed with it (see the ``finally``).
                    await _take_off_the_queue(deps.storage_provider, queued_message_id)
                await _mark_canvas_read(deps, session_id, digest, extra_payload)

            # 2. Re-arm the scheduler-visible row: claimable + clear pause;
            #    CREATED/PAUSED/WAITING advance to RUNNING (like /resume).
            # 01a08c08: a human-intent wake DOES still clear an existing pause
            # here (Q1's trace: "typing a message while paused means resume"
            # is the right call for a live human) -- but it must not do so
            # silently. was_paused is recorded below via PAUSE_SUPERSEDED so an
            # operator returning to the session can see that their pause was
            # superseded, and by what.
            was_paused = decided.pause_requested
            landed, settled = await _write_the_wake(
                sessions, row, decided=decided, instruction=instruction, human_intent=human_intent, peer=peer,
                external_tools=external_tools, armed=armed,
            )
            if landed is None:
                # A Cancel or a pause landed on the row after the decision (on any process): it wins, the session is not woken (a turn this
                # wake took is given back in the ``finally``). A message already written stays in the log, answered when the session is
                # resumed or reopened. Or another writer took the turn: its turn reads the message.
                return await sessions.get(session_id) or row
            row = landed

            # 3. Pulse the scheduler + claim engine so a worker runs the turn. The row is claimable from here on, so the claim is armed
            #    whatever the scheduler's hint does (a row left claimable with no lease is re-armed by the session-messaging sweep).
            try:
                await deps.scheduler.enqueue(session_id)
            finally:
                if deps.claim_engine is not None:
                    await deps.claim_engine.upsert(ClaimKind.SESSION, session_id)
        finally:
            if armed is not None and settled not in ("promoted", "taken"):
                await _settle_the_arm(
                    sessions, deps, session_id, armed=armed, to_a_pause=settled == "paused", message_written=message_written,
                )
        if deps.event_bus is not None:
            try:
                await deps.event_bus.publish(
                    f"session:{session_id}:claimable", {}
                )
            except Exception:  # noqa: BLE001 -- never break the wake
                logger.exception(
                    "wake_session: failed to publish claimable for %s",
                    session_id,
                )
            # Tick so the workspace tap surfaces the just-persisted USER_INPUT
            # record live (reset_session publishes the same event for its
            # invocation divider). Advisory: never break the wake.
            if user_input_seq is not None:
                try:
                    await deps.event_bus.publish(
                        f"session:{session_id}:tick", {"seq": user_input_seq}
                    )
                except Exception:  # noqa: BLE001 -- advisory
                    logger.exception(
                        "wake_session: failed to publish tick for %s",
                        session_id,
                    )
        if was_paused:
            await _record_pause_superseded(
                deps, workspace_id=workspace_id, session_id=session_id,
                row=row, action="cleared", pending_id=None,
            )
        return row


#: How many times a wake decides again after its conditional write was refused (another writer moved the row since its read). A refusal is a
#: real change each time (a turn armed, a record's seq taken, a Cancel, a pause), so a handful is plenty; the routed message is queued after that.
_ARM_ATTEMPTS = 5


async def _arm_or_reserve(sessions: Any, row: WorkspaceSession, *, arm: bool, owned: bool = False) -> WorkspaceSession | None:
    """Reserve seq ``row.last_seq + 1`` for the wake's USER_INPUT and, with ``arm``, take the turn, in ONE guarded ``patch_if``.

    Fenced on the row as read: its ``last_seq`` (the reservation), ``status`` and ``pause_requested`` (what the pause hold and the reopen
    decided from). With ``arm`` the write also takes the turn: fenced on no park and on ``turn_status`` idle, it sets ``arming`` and stamps
    ``armed_at`` (with ``owned`` this wake's own reopen already holds the turn: fenced on ``arming`` and that reopen's ``armed_at``, which it
    keeps). ``arming`` is a state every other wake treats as busy and that nothing claims or re-arms (audit G7-03, review H1), until
    :func:`_write_the_wake` promotes it to ``claimable`` once the message is written. Returns the row as written, or ``None`` when the fence
    was refused (the row changed: nothing was written). A row that is gone raises ``NotFoundError``.
    """
    where: dict[str, list[Any]] = {
        "last_seq": [raw_generation(row, "last_seq")],
        "status": [raw_generation(row, "status")],
        "pause_requested": [raw_generation(row, "pause_requested")],
    }
    patch: dict[str, Any] = {"last_seq": row.last_seq + 1}
    if arm:
        where["parked_status"] = [None]
        if owned:
            where["turn_status"] = ["arming"]
            where["armed_at"] = [raw_generation(row, "armed_at")]
        else:
            where["turn_status"] = ["idle"]
            patch["turn_status"] = "arming"
            patch["armed_at"] = datetime.now(timezone.utc)
    return await sessions.patch_if(row.id, to_jsonable_python(patch), where=where)


def _held_by(armed: WorkspaceSession) -> dict[str, list[Any]]:
    """The fence of a write only the wake that armed the turn may make: the turn is still ``arming`` under that wake's own ``armed_at``."""
    return {"turn_status": ["arming"], "armed_at": [raw_generation(armed, "armed_at")]}


async def _settle_the_arm(
    sessions: Any, deps: SessionWakeDeps, session_id: str, *, armed: WorkspaceSession, to_a_pause: bool, message_written: bool,
) -> None:
    """Settle a turn this wake took and did not promote (review N1): every exit after the arm comes here, the early returns included.

    * A pause or a Cancel won (``to_a_pause``): the turn goes back to ``idle``. A RUNNING row that a pause flag landed on goes ``PAUSED``
      with it, as the worker does at a turn boundary (no turn will come to honour the flag; ``/resume`` then runs the turn that answers a
      message already written: review N9).
    * The message is written and the wake did not get to promote it (its closing write failed, it was cancelled): the turn is promoted
      bare (``claimable``, and the claim armed), so the message is not left in the log of a session nothing will run; if that fails too the
      turn stays ``arming`` and the session-messaging sweep promotes it once it is stale (it reads the log).
    * Otherwise (no message was written): the turn goes back to ``idle``.

    Each write is fenced on the turn still being this wake's (:func:`_held_by`). Best effort and logged: the failure being reported, if any,
    is the caller's.
    """
    try:
        held = _held_by(armed)
        if to_a_pause:
            fresh = await sessions.get(session_id)
            if fresh is not None and fresh.status == SessionStatus.RUNNING and fresh.pause_requested:
                paused = await sessions.patch_if(
                    session_id, to_jsonable_python({"status": SessionStatus.PAUSED, "turn_status": "idle", "armed_at": None}),
                    where={**held, "status": [SessionStatus.RUNNING.value], "pause_requested": [True]},
                )
                if paused is not None:
                    return
        elif message_written:
            promoted = await sessions.patch_if(
                session_id, {"turn_status": "claimable", "armed_at": None},
                where={**held, "status": [s.value for s in (SessionStatus.CREATED, SessionStatus.RUNNING, SessionStatus.WAITING)]},
            )
            if promoted is not None:
                if deps.claim_engine is not None:
                    await deps.claim_engine.upsert(ClaimKind.SESSION, session_id)
                logger.warning("wake_session: session %s was promoted bare after the wake's closing write did not land", session_id)
                return
        undone = await sessions.patch_if(session_id, {"turn_status": "idle", "armed_at": None}, where=held)
        if undone is None:
            logger.info("wake_session: the turn armed for session %s was no longer this wake's when it was settled", session_id)
    except Exception:  # noqa: BLE001 -- the failure the caller reports (if any) is the one to report; a stale arm is healed by the sweep
        logger.warning("wake_session: could not settle the turn armed for session %s", session_id, exc_info=True)


async def _take_off_the_queue(storage_provider: Any, pending_id: str) -> None:
    """Delete a queued message whose USER_INPUT is written (review N4). One that is already gone is not an error."""
    from primer.model.workspace_session import PendingSessionMessage

    try:
        await storage_provider.get_storage(PendingSessionMessage).delete(pending_id)
    except NotFoundError:
        logger.debug("wake_session: queued message %s was already gone", pending_id)


async def _still_queued(storage_provider: Any, pending_id: str) -> bool:
    """Whether the queued message ``pending_id`` is still in the queue (no other drain delivered it since it was read)."""
    from primer.model.workspace_session import PendingSessionMessage

    return await storage_provider.get_storage(PendingSessionMessage).get(pending_id) is not None


async def _defer_routed(
    deps: SessionWakeDeps,
    *,
    row: WorkspaceSession,
    instruction: str,
    peer: PeerAttribution | None,
    by_a_person: bool,
    open_turn: str,
    receipt: SessionWakeReceipt,
    queued_message_id: str | None,
    extra_payload: dict | None = None,
) -> WorkspaceSession:
    """The routed message waits: the turn is open, or older messages are queued. ``skip`` drops it; a message the drain is realizing stays where
    it is in the queue; any other is stored at the back of the queue, carrying its sender (``queued_attribution``) and the canvas click it is
    (pivot P5)."""
    if queued_message_id is not None:
        receipt.outcome = "queued"
        return row
    if open_turn == "skip":
        receipt.outcome = "skipped_busy"
        return row
    from primer.session.pending_messages import store_pending_steer

    await store_pending_steer(
        storage_provider=deps.storage_provider,
        session=row,
        text=instruction,
        workspace_registry=deps.workspace_registry,
        event_bus=deps.event_bus,
        attribution=_attribution_to_queue(peer, by_a_person=by_a_person, extra_payload=extra_payload),
    )
    receipt.outcome = "queued"
    return row


async def _write_the_wake(
    sessions: Any,
    row: WorkspaceSession,
    *,
    decided: WorkspaceSession,
    instruction: str | None,
    human_intent: bool,
    peer: PeerAttribution | None,
    external_tools: "list | None",
    armed: WorkspaceSession | None,
) -> tuple[WorkspaceSession | None, str | None]:
    """The fields a wake owns, in ONE ``patch_if``: never a whole-row write of a copy read earlier, which put back whatever another process
    committed since (a Cancel's flag, a release's park, a reservation's ``last_seq``).

    ``row`` is the row as the wake's conditional write left it (or as read, for a wake without an instruction); ``decided`` is the row the
    decision was made on, before the arm. The write is fenced on the status the wake decided from; ``armed`` (the wake holds the turn as
    ``arming``) also fences it on the turn still being that wake's (``arming`` under its ``armed_at``), which it promotes to ``claimable``: a
    routed wake never writes claimable over a turn another writer took. A wake no person stands behind (``human_intent`` False) is also fenced
    on ``pause_requested`` being false, so a pause requested on another process since is never cleared by it (01a08c08).

    Returns ``(row as written, "promoted")``; or ``(None, why)`` when the session is not woken: ``"paused"`` (a Cancel ended it or a pause
    landed: they win, and the caller gives the turn back), ``"taken"`` (another writer took the turn: its turn reads the message), ``"gone"``
    (the row was deleted), ``None`` (the row kept changing). A turn the session-messaging sweep judged dead and gave back (``idle``, no
    ``armed_at``) while the message was being written is taken again and promoted (review N3): the message is in the log and must be run.
    Any other change (a status another writer moved) is written over again from a fresh read, a bounded number of times.
    """
    reclaim = False
    for _ in range(_ARM_ATTEMPTS):
        fields: dict[str, Any] = {
            "turn_status": "claimable",
            "pause_requested": False,
            "pause_requested_at": None,
        }
        if armed is not None:
            fields["armed_at"] = None
        # Replace the active external tool set for the turn this wake
        # triggers (None = leave the set unchanged; [] = clear it). Dumped
        # by_alias so the stored dicts carry the wire "schema" key.
        if external_tools is not None:
            fields["external_tools"] = [d.model_dump(by_alias=True) for d in external_tools]
        # A human who writes after pressing Stop has re-engaged, so that message wins over the
        # earlier Stop: left on the row, the flag would stop the very turn their message starts,
        # before its first token. Only when no turn is executing: a message sent WHILE a turn runs
        # is queued behind it, and the Stop is aimed at that running turn. And only a human's
        # message, never an automated wake (a trigger must not override a human's Stop).
        if human_intent and instruction and decided.turn_status != "running":
            fields["interrupt_requested"] = False
        if instruction:
            # The hop count of the message that starts this turn: a peer's, else 0. A person, a trigger and a channel start a fresh chain, so
            # the count never carries from one chain into the next. It rides this write, which is the one that arms the turn.
            fields["peer_hops"] = peer.hops if peer is not None else 0
        # Name the session from its opening instruction, once. Sessions
        # are the only conversational surface now, so the session list
        # needs the same readable titles the chat list had; a later
        # rename by the user must never be overwritten, hence the
        # None check rather than a blanket restamp.
        if instruction and row.name is None:
            fields["name"] = derive_title_from_text(instruction)
        now = datetime.now(timezone.utc)
        if row.status in _RESUMABLE:
            fields["status"] = SessionStatus.RUNNING
            if row.started_at is None:
                fields["started_at"] = now
        if row.last_turn_error is not None:
            # A row that carries a failure (C-024) is RUNNING with no lease row in the instant before the claim below is armed (a rested session this
            # wake moves to RUNNING), or already is (a failure exit that stamped and never finished), and its old start would make it what the
            # stuck-session sweeper reaps (turn 0, no lease, past the grace). The sweeper may be on another pod, so the fence is in the data:
            # restart the clock its age check reads and its write is fenced on, WHATEVER the status was.
            fields["started_at"] = now
        where: dict[str, list[Any]] = {"status": [raw_generation(row, "status")]}
        if armed is not None:
            where.update({"turn_status": ["idle"], "armed_at": [None]} if reclaim else _held_by(armed))
        if not human_intent:
            where["pause_requested"] = [False]
        try:
            written = await sessions.patch_if(row.id, to_jsonable_python(fields), where=where)
        except NotFoundError:
            return None, "gone"
        if written is not None:
            return written, "promoted"
        fresh = await sessions.get(row.id)
        if fresh is None:
            return None, "gone"
        paused_since = (fresh.status == SessionStatus.PAUSED and decided.status != SessionStatus.PAUSED) or (
            not human_intent and fresh.pause_requested
        )
        if fresh.status == SessionStatus.ENDED or paused_since:
            logger.info(
                "wake_session: session %s was %s after the wake decided; it is not woken", row.id,
                "paused" if paused_since else fresh.status.value,
            )
            return None, "paused"
        if armed is not None:
            ours = fresh.turn_status == "arming" and raw_generation(fresh, "armed_at") == raw_generation(armed, "armed_at")
            healed = fresh.turn_status == "idle" and fresh.armed_at is None
            if healed:
                logger.warning(
                    "wake_session: the turn of session %s was given back while its message was being written; it is taken again", row.id,
                )
            elif not ours:
                # Another writer took the turn from this wake (another wake armed it after the sweep gave it back): never written over.
                # The message is in the log; that writer's turn reads it.
                logger.warning(
                    "wake_session: the turn of session %s was taken from the wake that armed it (it reads %s); the wake does not write over it",
                    row.id, fresh.turn_status,
                )
                return None, "taken"
            reclaim = healed
        row = fresh
    logger.warning("wake_session: session %s kept changing under the wake's closing write; it was not written", row.id)
    return None, None


async def _record_pause_superseded(
    deps: SessionWakeDeps,
    *,
    workspace_id: str,
    session_id: str,
    row: WorkspaceSession,
    action: str,
    pending_id: str | None,
) -> None:
    """Append a PAUSE_SUPERSEDED record so a touched pause is never silent.

    Best-effort: a missing workspace / write failure is logged and
    swallowed, mirroring every other advisory write in this module
    (the divider tick above, wake_session's own claimable/tick publishes)
    -- an operator losing the ANNOUNCEMENT of a pause change must not
    also cost them the wake or the queued message it's reporting on.

    The record's seq is RESERVED on the row (``reserve_next_seq``) before it is written, and ``row.last_seq`` is brought up to it, so the
    next writer on the row does not repeat it (ticket 01a11cd8).
    """
    if deps.workspace_registry is None:
        return
    try:
        ws = await deps.workspace_registry.get_workspace(workspace_id)
        if ws is None:
            return
        reserved = await reserve_next_seq(deps.storage_provider.get_storage(WorkspaceSession), session_id)
        if reserved is None:
            logger.warning("wake_session: no seq could be reserved to record pause-superseded (%s) for %s", action, session_id)
            return
        row.last_seq = max(row.last_seq, reserved)
        writer = WorkspaceMessageWriter(
            workspace_io=ws, session_id=session_id, start_seq=reserved - 1,
        )
        seq = await writer.append(SessionMessageRecord(
            seq=1,  # overwritten by the writer's monotonic counter
            kind=SessionMessageKind.PAUSE_SUPERSEDED,
            payload={"action": action, "pending_id": pending_id},
            created_at=datetime.now(timezone.utc),
        ))
        await writer.flush()
    except Exception:  # noqa: BLE001 -- advisory, never block the wake
        logger.exception(
            "wake_session: failed to record pause-superseded (%s) for %s",
            action, session_id,
        )
        return
    if deps.event_bus is not None:
        try:
            await deps.event_bus.publish(
                f"session:{session_id}:tick", {"seq": seq}
            )
        except Exception:  # noqa: BLE001 -- advisory
            logger.exception(
                "wake_session: failed to publish pause-superseded tick for %s",
                session_id,
            )


__all__ = [
    "CanvasClickDoesNotReopen",
    "OnlyAPersonReopens",
    "SessionWakeDeps",
    "SessionWakeReceipt",
    "queued_attribution",
    "wake_session",
]
