"""Interactive webhook hold: fire, then await the fired runs' terminals.

S6 section 9 fixes the mechanism: the hold is an async wait on the
completion bus, never a poll. The subscription opens BEFORE the fire so a
run that reaches a terminal state while ``fire_trigger`` is still returning
cannot be missed.

The bus is the fast path but a completion NOTIFY is volatile (a Postgres
NOTIFY the process is not listening on when it is published, or a dropped
queue entry, never arrives). The session ROW is the durable state, so the
hold ALSO re-checks the pending rows on a bounded poll (review
2026-11-23 6.10): a lost NOTIFY is noticed within one poll interval
instead of waiting the caller's full wait_timeout and answering as a
timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.trigger.dispatch import FireResult, fire_trigger
from primer.trigger.subscribers import DispatchDeps

logger = logging.getLogger(__name__)

#: Hard upper bound on one hold, matching the worker's global yield cap
#: (primer/session/dispatch.py). The caller-visible wait cap is shorter and
#: is applied by the endpoint; this only stops an orphaned wait living
#: forever.
HOLD_MAX_SECONDS = 3600.0

_TERMINAL_PREFIX = "session:"
_TERMINAL_SUFFIX = ":terminal"

#: Cadence of the durable row re-check: how often the hold asks the
#: session ROW whether a pending run reached a terminal state when the
#: bus has delivered no terminal. The poll runs on a FIXED deadline that
#: events cannot move (a busy stream of other sessions' terminals must
#: not starve it, round 1 I3) and the deadline advances only after a
#: poll. Small enough that a lost NOTIFY is noticed promptly (the run's
#: row is updated by the worker on the same path that publishes the
#: wake), large enough to cost nothing on the normal bus-driven path.
_HOLD_ROW_POLL_SECONDS = 2.0

#: ``ended_reason`` values of a run that did not produce a result. The row is checked as well as the transcript
#: because the CANCELLED record of a cancelled turn is best-effort (skipped when the workspace does not take the
#: write in time), and without it the log reads as a completed turn whose answer the user had cancelled.
_NO_RESULT_ENDED_REASONS = frozenset({"cancelled", "failed", "force_deleted", "workspace_lost"})

#: The statuses the bus's ``:terminal`` announcement can carry. The
#: announcement is a TURN boundary (primer/session/dispatch.py::
#: _publish_terminal), never a lifecycle snapshot: every call site runs
#: after the turn's final row write, so it carries WAITING (a turn that
#: completed, or rested after a transport failure) or PAUSED (a pause that
#: landed while the turn ran) as well as ENDED - never CREATED/RUNNING.
_BOUNDARY_STATUSES = (SessionStatus.WAITING, SessionStatus.PAUSED)

#: How old a target's row may be for the poll's non-ENDED arm to apply
#: (J minor M3). The row alone cannot date a turn boundary, so the poll
#: completes a WAITING/PAUSED row only when the row was BORN around this
#: fire (the fire created the session) and the boundary is the FIRED
#: TURN'S OWN (round 2 M1): the row's turn_no one past the turn number
#: captured at fire time, since a row's turn_no moves only when a turn's
#: effects commit and a later turn (a user append or a pause during the
#: fired turn) may have reached the boundary first. A target that predates
#: the fire (an append into an idle session, a parked resume) may sit in
#: WAITING/PAUSED from BEFORE the fire, and its terminal is the
#: appended/resumed turn's: for such a target only ENDED (or a gone row)
#: completes the hold, as before (a lost NOTIFY there degrades to the wait
#: cap; the bus fast path is unaffected). A fresh row whose fired turn
#: failed or parked (neither bumps turn_no) degrades the same way on the
#: row-only path: its boundary is indistinguishable from a still-running
#: one, so the hold waits the cap there.
_FRESH_ROW_AGE_SECONDS = 60.0


def _terminal_session_id(event_key: str) -> str | None:
    """Return the session id inside ``session:<sid>:terminal``, else None."""
    if not event_key.startswith(_TERMINAL_PREFIX):
        return None
    if not event_key.endswith(_TERMINAL_SUFFIX):
        return None
    return event_key[len(_TERMINAL_PREFIX):-len(_TERMINAL_SUFFIX)] or None


def held_targets(result: FireResult) -> list[str]:
    """Artefacts the hold waits on: dispatched, non-skipped, id-bearing."""
    out: list[str] = []
    for envelope in result.results:
        if (
            envelope.get("ok")
            and not envelope.get("skipped")
            and envelope.get("artefact_id")
        ):
            out.append(envelope["artefact_id"])
    return out


@dataclass
class HeldFire:
    """Outcome of one held fire."""

    fire_result: FireResult | None = None
    results: list[dict] = field(default_factory=list)
    timed_out: bool = False


async def fire_and_hold(
    *,
    trigger_id: str,
    extra_context: dict,
    deps: DispatchDeps,
    workspace_registry: Any,
    wait_timeout: float,
    on_fired: Callable[[FireResult], Awaitable[None]] | None = None,
) -> HeldFire:
    """Fire ``trigger_id`` and hold until every fired run reaches a terminal.

    ``on_fired`` is awaited as soon as the fire returns, before the hold waits: the webhook endpoint records the fire
    on its delivery row there, so a recovery sweep that runs while the hold is still waiting does not fire again (W12).
    """
    if deps.event_bus is None:
        # No bus, no completion signal: fire and report the timeout shape so
        # the caller falls back to the 202 + poll path rather than claiming
        # a result it never observed.
        result = await fire_trigger(
            trigger_id=trigger_id, scheduled_for=None, deps=deps,
            extra_context=extra_context,
        )
        if on_fired is not None:
            await on_fired(result)
        return HeldFire(fire_result=result, timed_out=True)

    sub = deps.event_bus.subscribe(prefix=_TERMINAL_PREFIX, suffix=_TERMINAL_SUFFIX)  # only terminal events: nothing else can be dropped for them
    await sub.ready()  # listening before the fire publishes its wake
    try:
        result = await fire_trigger(
            trigger_id=trigger_id, scheduled_for=None, deps=deps,
            extra_context=extra_context,
        )
        if on_fired is not None:
            await on_fired(result)
        targets = held_targets(result)
        if not targets:
            return HeldFire(fire_result=result)
        pending = set(targets)
        # Capture each target's age and turn number at fire time: the poll's
        # non-ENDED arm (_BOUNDARY_STATUSES) applies only to rows this fire
        # brought into a live state (a fresh session) and only to the FIRED
        # TURN'S OWN boundary (round 2 M1): a row born around the fire may
        # carry a LATER turn's boundary (a user append or a pause during the
        # fired turn), and the row alone cannot date a boundary - but a
        # row's turn_no moves only when a turn's effects commit, so the
        # fired turn's boundary is the one with turn_no one past the fire's.
        # A row that predates the fire may sit in WAITING/PAUSED from before;
        # only ENDED (or a gone row) completes such a target. A failed read
        # keeps the target on the ENDED-only poll (the pre-M3 behaviour),
        # never the other way round.
        fresh_at_fire: dict[str, bool] = {}
        turn_no_at_fire: dict[str, int] = {}
        fire_now = datetime.now(timezone.utc)
        for sid in pending:
            try:
                row0 = await deps.storage_provider.get_storage(
                    WorkspaceSession,
                ).get(sid)
            except Exception:  # noqa: BLE001 - a failed read must not abort the hold
                row0 = None
            if row0 is None or row0.created_at is None:
                fresh_at_fire[sid] = False
                continue
            created = row0.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            fresh_at_fire[sid] = (
                (fire_now - created).total_seconds() <= _FRESH_ROW_AGE_SECONDS
            )
            turn_no_at_fire[sid] = row0.turn_no
        timed_out = False
        sub_alive = True
        # The bus as a task: the fast path. Drained one event at a time so
        # a poll timeout below can also be decided (an ``async for`` cannot
        # be raced against a clock without re-implementing the wait).
        event_task = asyncio.create_task(_next_event(sub))
        try:
            # A FIXED poll deadline, not a timer re-armed by every event
            # (round 1 I3): while other sessions kept ending every <2 s
            # the re-armed 2 s wait never expired and the row poll never
            # ran. Events decide the fast path; the deadline alone
            # decides the poll, and it advances only after a poll.
            next_poll_at = time.monotonic() + _HOLD_ROW_POLL_SECONDS
            async with asyncio.timeout(wait_timeout):
                while pending:
                    if sub_alive:
                        done, _ = await asyncio.wait(
                            {event_task},
                            timeout=max(0.0, next_poll_at - time.monotonic()),
                        )
                        if event_task in done and not event_task.cancelled():
                            try:
                                event = event_task.result()
                            except StopAsyncIteration:
                                event = None
                            if event is not None:
                                sid = _terminal_session_id(event.event_key)
                                if sid is not None and sid in pending:
                                    pending.discard(sid)
                                event_task = asyncio.create_task(_next_event(sub))
                            else:
                                # The subscription itself ended: no event
                                # will ever arrive again, so the deadline
                                # below paces the row polls by hand.
                                sub_alive = False
                    elif time.monotonic() < next_poll_at:
                        await asyncio.sleep(next_poll_at - time.monotonic())
                    if time.monotonic() >= next_poll_at:
                        # The wake may have been lost (a NOTIFY never
                        # delivered to this process). The session ROW is
                        # the durable state, so re-check the pending
                        # rows. One failed read keeps the session pending
                        # and keeps waiting (round 1 I4): it must not
                        # abort a hold whose fire already happened.
                        for sid in list(pending):
                            try:
                                row = await deps.storage_provider.get_storage(
                                    WorkspaceSession,
                                ).get(sid)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "webhook hold: row poll read failed for %s",
                                    sid, exc_info=True,
                                )
                                continue
                            # The bus treats a turn boundary as terminal with
                            # WHATEVER status _publish_terminal carried (J
                            # minor M3): ENDED, or a WAITING/PAUSED row born
                            # by this fire whose boundary is the FIRED TURN'S
                            # OWN (round 2 M1): the row's turn_no one past
                            # the turn number captured at fire time, so a
                            # later turn's boundary (a user append or a
                            # pause during the fired turn) is not passed off
                            # as the fired run's result. A row older than the
                            # fire keeps the ENDED-only rule: its WAITING/
                            # PAUSED may predate the fired run.
                            if (
                                row is None
                                or row.status == SessionStatus.ENDED
                                or (
                                    row.status in _BOUNDARY_STATUSES
                                    and fresh_at_fire.get(sid, False)
                                    and row.turn_no == turn_no_at_fire.get(sid, -1) + 1
                                )
                            ):
                                pending.discard(sid)
                        next_poll_at = time.monotonic() + _HOLD_ROW_POLL_SECONDS
        except TimeoutError:
            timed_out = True
        finally:
            event_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await event_task
    finally:
        await sub.aclose()

    if timed_out:
        return HeldFire(fire_result=result, timed_out=True)

    results = []
    for sid in targets:
        results.append({
            "artefact_id": sid,
            "final_text": await _final_text(
                deps.storage_provider, workspace_registry, sid,
            ),
        })
    return HeldFire(fire_result=result, results=results)


async def _next_event(sub: Any) -> Any:
    """Yield the subscription's next event (the bus's fast path as a task)."""
    async for event in sub:
        return event
    raise StopAsyncIteration


async def _final_text(
    storage_provider: Any, workspace_registry: Any, session_id: str,
) -> str | None:
    """Derive the run's final text exactly as the channel relay does."""
    if workspace_registry is None:
        return None
    # Function-local import: primer.channel is an optional subsystem, so the
    # trigger layer reaches it lazily (mirrors primer/session/dispatch.py).
    from primer.channel.session_relay import read_session_final_text

    try:
        row = await storage_provider.get_storage(WorkspaceSession).get(
            session_id
        )
        if row is None:
            return None
        if row.status == SessionStatus.ENDED and row.ended_reason in _NO_RESULT_ENDED_REASONS:
            return None
        workspace = await workspace_registry.get_workspace(row.workspace_id)
        if workspace is None:
            return None
        return await read_session_final_text(workspace, session_id)
    except Exception:  # noqa: BLE001 - a read failure must not fail the hold
        logger.warning(
            "webhook hold: final-text derivation failed for %s", session_id,
            exc_info=True,
        )
        return None


__all__ = ["HOLD_MAX_SECONDS", "HeldFire", "fire_and_hold", "held_targets"]
