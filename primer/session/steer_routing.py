"""Routing decision for an inbound steer: wake now, or queue it.

A session runs one turn at a time, so a steer that lands while a turn is
still open cannot become a second USER_INPUT record without breaking the
1:1 pairing the drain counts (primer/session/turns.py). This module makes
that call in one place, so the API path and any machine entrypoint route
identically.
"""

from __future__ import annotations

from primer.model.workspace_session import WorkspaceSession
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

    Session status is deliberately NOT consulted. It is coarser: a
    RUNNING session commonly sits idle between turns, so treating it as
    busy would defer steers that should have run immediately.

    ``raw_lines`` is an optional slow path for callers that already
    hold the log, such as the drain. It closes the small window where
    turn_status has gone idle but the terminal record has not landed.
    Omitting it only forgoes that extra check.
    """
    if row.turn_status in ("claimable", "running"):
        return ROUTE_PENDING
    if row.parked_status is not None:
        return ROUTE_PENDING
    if raw_lines and has_open_turn(raw_lines, cursor=row.next_unprocessed_seq):
        return ROUTE_PENDING
    return ROUTE_WAKE


__all__ = ["ROUTE_PENDING", "ROUTE_WAKE", "route_steer"]
