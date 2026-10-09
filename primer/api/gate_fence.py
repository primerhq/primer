"""The fence on a human decision: it must name the gate it answers (console review C-033, ticket 01a11f52-9d98).

A provider repeats its ``tool_call_id`` across rounds, and the approval and ask_user event keys are built from it, so the raw id alone cannot say
WHICH gate a decision is for: a card left open for round 1's gate decided whatever gate was pending under the same id in round 3. Every such gate
now carries a ``gate_id`` minted when it was created (:func:`primer.model.yield_.new_gate_id`, stored in the pending entry's ``resume_metadata``),
the pending responses serve it, and the respond routes take it back as ``gate_id``. A respond naming a gate that is no longer the pending one
is refused with a 409 ``approval_stale`` BEFORE anything moves.

A respond that names none is still accepted (a client that predates the token, a channel tag minted before this release): logged once without
the call's arguments and counted in ``gate_respond_total{token="absent"}`` (:func:`primer.session.gate_token.count_gate_token`, shared with the
channel inbox), so the flip to a 422 can be scheduled once that count is zero.
"""

from __future__ import annotations

from fastapi import HTTPException

from primer.session.gate_token import count_gate_token


APPROVAL_STALE = "approval_stale"
"""The RFC 7807 ``code`` of a respond that named a gate that has since been replaced (409). One code for approvals and ask_user alike."""

_NOUN = {"approval": "approval", "ask_user": "question"}


def stale_gate_error(kind: str) -> HTTPException:
    """The 409 for a respond that named a gate that is no longer the pending one; ``kind`` is ``approval`` or ``ask_user``."""
    return HTTPException(
        status_code=409,
        detail={
            "code": APPROVAL_STALE,
            "message": f"this {_NOUN.get(kind, 'request')} was replaced by a newer one; reload the pending list",
        },
    )


__all__ = ["APPROVAL_STALE", "count_gate_token", "stale_gate_error"]
