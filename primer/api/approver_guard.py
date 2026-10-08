"""The HTTP face of the shared approver check (ticket 01a11b64).

Every REST route that can decide an approval gate (the respond route, and the yield-cancel route, because cancelling an ``_approval`` gate is
classified as a rejection) judges the caller with this one function, which wraps :func:`primer.session.approvers.may_decide`.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from primer.session.approvers import APPROVER_MISMATCH, may_decide


def enforce_approvers(metadata: dict, user: Any) -> None:
    """403 ``approver_mismatch`` unless the caller may decide the gate whose ``resume_metadata`` is ``metadata``.

    ``metadata`` MUST be the ``resume_metadata`` of the SPECIFIC pending gate, taken from
    :func:`~primer.session.pending_gates.resolve_pending_gate` (a graph park can hold several gates, each with its own stamped spec). Never
    pass the top-level ``yielded.resume_metadata`` of a parked blob: for a graph park it is a key-less projection of the primary gate (no
    ``approvers``), which reads as "anyone". A route that cannot resolve the gate passes
    :data:`~primer.session.approvers.ADMIN_ONLY_METADATA`. No stamped spec means anyone; admins always pass; a spec that cannot be read
    fails CLOSED to admin-only; a call_tool park from before the stamp existed is admin-only (see
    :func:`~primer.session.approvers.may_decide`).
    """
    if user is None:  # WS scope / auth-disabled synthetic admin absent
        return
    if not may_decide(
        metadata,
        username=getattr(user, "username", None),
        role=getattr(user, "role", None),
    ):
        raise HTTPException(
            status_code=403,
            detail={"error": APPROVER_MISMATCH},
        )


__all__ = ["enforce_approvers"]
