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


def route_steer(row: WorkspaceSession, *, raw_lines: list[str]) -> str:
    """Return ROUTE_PENDING when a turn is open, else ROUTE_WAKE.

    The row carries the fast path: a claimable or running turn_status,
    or any parked_status, means a turn is in flight. The log is the slow
    path behind it, because the row can read idle for a turn whose
    terminal record has not landed yet, and queueing on the row alone
    would let that window produce a colliding second user message.
    """
    if row.turn_status in ("claimable", "running"):
        return ROUTE_PENDING
    if row.parked_status is not None:
        return ROUTE_PENDING
    if has_open_turn(raw_lines, cursor=row.next_unprocessed_seq):
        return ROUTE_PENDING
    return ROUTE_WAKE


__all__ = ["ROUTE_PENDING", "ROUTE_WAKE", "route_steer"]
