"""The Person: the user behind a caller, and the two derivations of it (access-control spec 5.1).

Access decisions never run against a raw principal. ``person_from_request``
turns a live request's ``state.actor`` into the acting person (an
``api_token`` actor is the user who owns the token, never the token
itself); ``person_from_ref`` does the same for a persisted
``PrincipalRef`` initiator. Roles: ``admin`` and the auth-disabled
``system`` principal act as ``admin``; a live request with no actor acts
as ``restricted`` (the onboarding gate); a missing or unattributed ref
acts as ``user`` with no ``user_id``, so it reads nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from primer.model.principal import PrincipalRef

if TYPE_CHECKING:
    from primer.model.yield_ import ToolContext


@dataclass(frozen=True)
class Person:
    """The user an access decision runs against, plus their role."""

    user_id: str | None
    role: str

    @property
    def is_admin(self) -> bool:
        """Admins and the system principal (auth disabled) clear every gate."""
        return self.role in ("admin", "system")

    @property
    def is_full_user(self) -> bool:
        """A user who may read public/shared rows; ``restricted`` may not."""
        return self.role in ("user", "admin", "system")


def person_from_request(request: Request) -> Person:
    """Derive the acting person from a live request's actor principal."""
    actor = getattr(request.state, "actor", None)
    if actor is None:
        # Auth enabled but nobody authenticated: the onboarding gate.
        return Person(None, "restricted")
    if actor.type == "system":
        # Auth-disabled install: the synthetic system principal acts.
        return Person(None, "system")
    return Person(actor.user_id, actor.role or "user")


def person_from_ref(ref: PrincipalRef | None) -> Person:
    """Derive the acting person from a persisted initiator projection."""
    if ref is None or ref.id == "unattributed":
        # The unattributed() stand-in names nobody: no user, no visibility.
        return Person(None, "user")
    if ref.type == "system":
        return Person(None, "system")
    user_id = ref.user_id
    if user_id is None and ref.type == "user":
        # Legacy row persisted before Principal carried user_id: the ref's
        # own id IS the user id.
        user_id = ref.id
    return Person(user_id, ref.role or "user")


def person_for_tool(ctx: "ToolContext | None") -> Person:
    """The person behind a tool call: the run's initiator when the call carries a ``ToolContext``, else the MCP request's actor.

    The MCP endpoint dispatches handlers without a ``ToolContext`` and publishes its live actor on
    :data:`primer.mcp.server.current_actor` instead. Nothing known reads only public rows.
    """
    if ctx is not None:
        return person_from_ref(ctx.initiated_by)
    from primer.agent.invoke import _RUN_ID  # lazy: the agent package imports this one
    from primer.mcp.server import current_actor  # lazy: the MCP server module is heavy and optional on this path

    # A subagent started over MCP carries no context either, but its run is not the MCP person's act: it holds nothing the person's rank or levels
    # would give it (the unattributed run of ``PrincipalRef.unattributed``), so only public rows read.
    if _RUN_ID.get() is not None:
        return Person(None, "user")
    actor = current_actor.get()
    if actor is None:
        return Person(None, "user")
    if actor.type == "system":
        return Person(None, "system")
    return Person(actor.user_id, actor.role or "user")
