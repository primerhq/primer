"""Routing decision for an inbound steer: wake now, or queue it.

A session runs one turn at a time, so a steer that lands while a turn is
still open cannot become a second USER_INPUT record without breaking the
1:1 pairing the drain counts (primer/session/turns.py). This module makes
that call in one place, so the API path and any machine entrypoint route
identically.
"""

from __future__ import annotations

from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.turns import has_open_turn

ROUTE_PENDING = "pending"
ROUTE_WAKE = "wake"


def route_steer(
    row: WorkspaceSession, *, raw_lines: list[str] | None = None
) -> str:
    """Return ROUTE_PENDING when a turn is open, else ROUTE_WAKE.

    turn_status is the authoritative per-turn flag: claimable means a
    turn is armed, running means one is executing, idle means neither.
    A parked session counts as busy too, since it waits on a human
    rather than being finished.

    Session status is deliberately NOT consulted to find a turn that is
    open: it is coarser, and a RUNNING session commonly sits idle between
    turns, so treating it as busy would defer steers that should have run
    immediately. The one status that matters is ENDED, and it decides the
    other way: an ended session has no turn to wait behind, whatever its
    park columns say and whatever its turn_status reads (a row ended while
    its flag still says ``running`` or ``claimable`` is woken too: the
    turn is over, the flag is stale, and the reopen resets it), so it is
    always woken, which reopens it or refuses it with a conflict. Two
    real writers end a session and leave the park
    set (Cancel of a paused or waiting row, which deletes the lease so no
    release ever clears it, and the workspace_lost reconcile, which does
    not touch the park), and a steer queued behind that park would never be
    delivered. (The pool's ``_end_session`` also writes ENDED while the row
    still carries its park until the release; a steer that lands in that
    window reopens the session itself, and the one queued steer
    ``_end_session`` realizes right after is written behind it. A binding
    switch queued on that parked session is not applied by ``_end_session``
    either: the reopened row no longer passes the applier's
    ``{"status": ["ended"]}`` guard, so the switch stays queued and applies
    at the checkpoint after the turn that answers both messages, which
    therefore runs under the OUTGOING binding.)

    ``raw_lines`` is an optional slow path for callers that already
    hold the log, such as the drain. It closes the small window where
    turn_status has gone idle but the terminal record has not landed.
    Omitting it only forgoes that extra check.
    """
    if row.status == SessionStatus.ENDED:
        return ROUTE_WAKE
    if row.turn_status in ("claimable", "running"):
        return ROUTE_PENDING
    if row.parked_status is not None:
        return ROUTE_PENDING
    if raw_lines and has_open_turn(raw_lines, cursor=row.next_unprocessed_seq):
        return ROUTE_PENDING
    return ROUTE_WAKE


__all__ = ["ROUTE_PENDING", "ROUTE_WAKE", "route_steer"]
