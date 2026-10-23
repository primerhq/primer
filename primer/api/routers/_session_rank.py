"""The rank rule of the REST routes that act on a session (lead rulings 2 and 3 of the G7 fixes; review of #853).

A session runs at its creator's rank, so a steer, a restart, a cancel or a resume of it acts on a run at that rank: the caller must rank at least as
high as the creator, the rule a trigger's fire and a message from another session follow (:func:`primer.authz.may_drive`), or the route answers 403
``forbidden_rank`` with one fixed message that names neither the creator nor its rank. A caller acting on a session its own account or token started
is never refused (``own_session``: the request carries the account's current role, which caps every run of it). People keep their reach over every
workspace: this is a rank rule, not a scope.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def refuse_outranked(request: Request, row: Any) -> None:
    """Raise 403 ``forbidden_rank`` when the request's actor may not act on the session ``row``."""
    from primer.authz import OUTRANKED_MESSAGE, may_drive

    if not may_drive(getattr(request.state, "actor", None), row.initiated_by, own_session=True):
        raise HTTPException(status_code=403, detail={"error": "forbidden_rank", "message": OUTRANKED_MESSAGE})


__all__ = ["refuse_outranked"]
