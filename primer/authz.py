"""Shared RBAC role-floor predicate.

Two independent dispatch paths enforce the SAME per-tool ``required_role``
floor and must agree bit-for-bit:

* the MCP dispatch path
  (:func:`primer.mcp.dispatch.invoke_exposed`), authorised against the
  per-request :class:`primer.model.principal.Principal` (the ``actor``);
* the agent tool-execution path
  (:meth:`primer.agent.tool_manager.ToolExecutionManager._dispatch_toolset`),
  authorised against the run's persisted invoker
  (:class:`primer.model.principal.PrincipalRef`, the ``initiated_by``).

The predicate lives here so there is exactly one copy of the ranking and
the always-allow rules. It is deliberately DUCK-TYPED: it reads only
``.type`` and ``.role``, so it accepts either a live ``Principal`` or its
persisted projection ``PrincipalRef`` without importing or branching on
the concrete class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primer.model.principal import Principal, PrincipalRef


_ROLE_RANK = {"restricted": 0, "user": 1, "admin": 2}


def _role_allows(actor: "Principal | PrincipalRef | None", need: str) -> bool:
    """True iff ``actor`` may invoke a tool requiring role ``need``.

    Always-allow actors (keyed on ``type``):

    * ``system`` -- the auth-disabled / internal bypass principal.
    * ``trigger`` -- an internal automation actor: a trigger-fired run
      identified by its trigger id, carrying no human ``role``. A trigger
      is trusted internal automation and is allowed through the floor
      exactly like ``system``.

    We key on ``type`` (NOT ``source == "internal"``): an ``api_token``
    actor is also ``source == "internal"`` but must keep being ranked by
    the owning user's real ``role``, never waved through.

    A missing actor never passes. Otherwise the actor's declared ``role``
    must rank at or above ``need`` in :data:`_ROLE_RANK`; an
    unranked/unknown role loses the comparison (``-1``) and an
    unranked/unknown ``need`` can never be satisfied (``99``) -- both fail
    closed.
    """
    if actor is None:
        return False
    if actor.type in ("system", "trigger"):
        return True
    return _ROLE_RANK.get(actor.role, -1) >= _ROLE_RANK.get(need, 99)


__all__ = ["_ROLE_RANK", "_role_allows"]
