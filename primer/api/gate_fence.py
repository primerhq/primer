"""The fence on a human decision: it must name the gate it answers (console review C-033, ticket 01a11f52-9d98).

A provider repeats its ``tool_call_id`` across rounds, and the approval and ask_user event keys are built from it, so the raw id alone cannot say
WHICH gate a decision is for: a card left open for round 1's gate decided whatever gate was pending under the same id in round 3. Every such gate
now carries a ``gate_id`` minted when it was created (:func:`primer.model.yield_.new_gate_id`, stored in the pending entry's ``resume_metadata``),
the pending responses serve it, and the respond routes take it back as ``gate_id``. A respond naming a gate that is no longer the pending one
is refused with a 409 ``approval_stale`` BEFORE anything moves.

A respond that names none is still accepted (a client that predates the token, a channel tag minted before this release): logged once without
the call's arguments and counted in ``gate_respond_total{gate_token="absent"}`` (:func:`primer.session.gate_token.count_gate_token`, shared with the
channel inbox), so the flip to a 422 can be scheduled once that count is zero.

A gate takes ONE decision (ticket 01a12606): a respond for a gate that already holds one (another operator's, or this client's own earlier
different answer) is refused by the durable flip and answered 409 ``already_decided``; nothing moves, and the first decision is the one that
runs and the one the audit names. The same decision sent again is the one that landed, and is accepted again.
"""

from __future__ import annotations

from fastapi import HTTPException

from primer.session.gate_token import count_gate_token


APPROVAL_STALE = "approval_stale"
"""The RFC 7807 ``code`` of a respond that named a gate that has since been replaced (409). One code for approvals and ask_user alike."""

ALREADY_DECIDED = "already_decided"
"""The RFC 7807 ``code`` of a decision on a gate that already holds one (409): the decision was not applied, recorded or published. One code for
approvals and ask_user alike."""

_NOUN = {"approval": "approval", "ask_user": "question", "yield": "yield"}


def stale_gate_error(kind: str, *, queued: bool = False) -> HTTPException:
    """The 409 for a respond or cancel that named a gate or yield that is no longer the pending one.

    ``kind`` is ``approval``, ``ask_user`` or ``yield`` (a park that is not a human gate: sleep, watch_files, an external wait); the words
    follow it. ``queued`` is a cancel that named a gate which is still pending but is not the one the route reaches: a graph park cancels the
    first pending entry under a call id, so a sibling queued behind it is not "replaced", it has to be decided with its own respond.
    """
    noun = _NOUN.get(kind, "request")
    if queued:
        message = f"this {noun} is queued behind another one of the same session, which is the one a cancel reaches; decide it with its own response"
    else:
        message = f"this {noun} was replaced by a newer one; reload the pending list"
    return HTTPException(status_code=409, detail={"code": APPROVAL_STALE, "message": message})


def decided_gate_error(kind: str) -> HTTPException:
    """The 409 for a decision the durable flip refused: the gate (``approval`` or ``ask_user``) already holds a decision, which stands."""
    noun = _NOUN.get(kind, "request")
    verb = "answered" if kind == "ask_user" else "decided"
    return HTTPException(status_code=409, detail={"code": ALREADY_DECIDED, "message": f"this {noun} was already {verb}; reload the pending list"})


__all__ = ["ALREADY_DECIDED", "APPROVAL_STALE", "count_gate_token", "decided_gate_error", "stale_gate_error"]
