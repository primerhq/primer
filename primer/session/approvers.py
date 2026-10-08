"""Who may decide an approval gate: ONE enforcement for every path that answers one (ticket 01a11b64).

A gate is parked with the approver spec that applies to it (the policy row's, or the evaluator's per-call override) stamped into the
park's ``resume_metadata["approvers"]`` (:func:`primer.agent.approval.approval_resume_metadata` builds that metadata for every approval
park). An answer must be judged against THAT gate's stamped spec, whichever surface it arrives on: the REST respond route, a channel
inbox reply, any tool that answers a gate. Each surface used to carry its own idea of this check (or none), so a spec was enforced on one
and silently ignored on the others.

The rule, in :func:`may_decide`:

* no stamped spec, or ``kind == "anyone"``: anyone who can answer may decide it;
* otherwise the decider must be IDENTIFIED (a primer user with a username and a role) and admitted by :meth:`ApproverSpec.allows`
  (admins are always admitted, whatever the kind);
* an UNIDENTIFIED decider (a chat-platform user whose id maps to no primer account) is admitted only when the spec is ``anyone``: a
  restricted gate cannot be proven to be answered by someone it admits, so it fails closed and is decided in the console;
* a stored spec that cannot be read fails CLOSED to admin-only (it is stamped by our own code, so this is corruption; admins are always
  admitted, so it cannot wedge a park);
* a ``call_tool`` park (it carries ``via_call_tool``) whose ``approvers`` KEY is absent was parked before the stamp existed: admin-only too.

What to pass as ``metadata``: the ``resume_metadata`` of the SPECIFIC pending gate, taken from
:func:`primer.session.pending_gates.resolve_pending_gate` (a graph park can hold several gates, each with its own stamp). NEVER judge the
top-level ``parked_state["yielded"]`` of a graph park directly: it is a projection of the primary gate
(``_CheckpointMixin._build_pending_park_yield``) and, when that primary is a ToolCall node's approval, it carries ``original_call`` and
nothing else, so it is key-less (no ``approvers``, no ``via_call_tool``) and reads as "anyone" whatever the gate says. A caller that
cannot resolve the gate has nothing to judge and passes :data:`ADMIN_ONLY_METADATA`, which fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from primer.model.except_ import PrimerError
from primer.model.tool_approval import ApproverSpec

logger = logging.getLogger(__name__)

#: The REST error code for a refused decider (``403`` body ``{"error": "approver_mismatch"}``).
APPROVER_MISMATCH = "approver_mismatch"

#: The spec only an admin satisfies (no role and no user is named; admins are always admitted).
ADMIN_ONLY_SPEC: dict[str, Any] = {"kind": "roles", "roles": [], "users": []}

#: What to judge a decider against when the gate cannot be resolved or read: admin-only, never open.
ADMIN_ONLY_METADATA: dict[str, Any] = {"approvers": ADMIN_ONLY_SPEC}


class ApproverRefusedError(PrimerError):
    """The decider is not one the gate's approver spec admits; nothing was decided."""


def may_decide(metadata: Mapping[str, Any] | None, *, username: str | None, role: str | None) -> bool:
    """Whether the decider may decide the gate whose ``resume_metadata`` is ``metadata``.

    ``username`` / ``role`` are ``None`` for a decider that is not a known primer user (see the module docstring).
    """
    meta = metadata or {}
    raw = meta.get("approvers")
    if "approvers" not in meta and "via_call_tool" in meta:
        # A call_tool park written before the stamp existed (only that park writes `via_call_tool`): the key is ABSENT, not None, and
        # absent used to read as anyone, leaving the gate open to every user until it timed out. Whether it was restricted cannot be
        # known now, so it fails closed. An explicit None means anyone; agent-loop parks have always written the key since P6.
        raw = ADMIN_ONLY_SPEC
    if not raw:
        return True
    try:
        spec = ApproverSpec.model_validate(raw)
    except Exception:  # noqa: BLE001 - corrupt stamp: fail closed, never open
        logger.warning("unreadable stored approvers %r; only an admin may decide this gate", raw)
        spec = ApproverSpec(kind="roles", roles=[])
    if spec.kind == "anyone":
        return True
    if username is None or role is None:
        return False
    return spec.allows(username=username, role=role)


def ensure_may_decide(metadata: Mapping[str, Any] | None, *, username: str | None, role: str | None) -> None:
    """Raise :class:`ApproverRefusedError` unless :func:`may_decide`."""
    if not may_decide(metadata, username=username, role=role):
        raise ApproverRefusedError(
            "this approval is routed to specific approvers and the decider is not one of them; decide it in the console as an "
            "approver or an admin"
        )


__all__ = ["ADMIN_ONLY_METADATA", "ADMIN_ONLY_SPEC", "APPROVER_MISMATCH", "ApproverRefusedError", "ensure_may_decide", "may_decide"]
