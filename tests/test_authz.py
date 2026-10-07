"""Unit tests for the shared RBAC role-floor predicate.

``primer.authz._role_allows`` is the single source of truth for both the
MCP dispatch floor (:func:`primer.mcp.dispatch.invoke_exposed`) and the
agent tool-execution floor
(:meth:`primer.agent.tool_manager.ToolExecutionManager._dispatch_toolset`).
It is duck-typed on ``.type`` / ``.role`` so it accepts either a live
:class:`Principal` or its persisted :class:`PrincipalRef` projection.

Mirrors the style of ``tests/mcp/test_dispatch_rbac.py`` -- a role matrix
plus the always-allow / fail-closed edge cases. Only ``system`` is
always allowed; a ``trigger`` actor is ranked by its role (A-20).
"""

from __future__ import annotations

import pytest

from primer.authz import _ROLE_RANK, _role_allows
from primer.model.principal import Principal, PrincipalRef


def _user(role: str | None) -> Principal:
    return Principal(
        type="user", id="u", display="u", role=role, source="local",
    )


@pytest.mark.parametrize(
    ("role", "need", "allowed"),
    [
        ("admin", "admin", True),
        ("admin", "user", True),
        ("admin", "restricted", True),
        ("user", "admin", False),
        ("user", "user", True),
        ("user", "restricted", True),
        ("restricted", "admin", False),
        ("restricted", "user", False),
        ("restricted", "restricted", True),
        (None, "restricted", False),   # unknown/unset role ranks -1
        ("user", "bogus", False),      # unknown need ranks 99, never met
    ],
)
def test_user_actor_ranked_against_need(role, need, allowed) -> None:
    """A ``user``-type actor is gated strictly by ``_ROLE_RANK``."""
    assert _role_allows(_user(role), need) is allowed


def test_missing_actor_denied() -> None:
    """No actor never passes, even the lowest floor."""
    assert _role_allows(None, "restricted") is False


def test_system_actor_always_allowed() -> None:
    """The system (auth-disabled) principal clears any floor."""
    actor = Principal(
        type="system", id="s", display="s", role=None, source="system",
    )
    assert _role_allows(actor, "admin") is True


@pytest.mark.parametrize(
    ("role", "need", "allowed"),
    [
        (None, "admin", False),
        (None, "restricted", False),
        ("user", "admin", False),
        ("user", "user", True),
        ("admin", "admin", True),
    ],
)
def test_trigger_actor_is_ranked_by_its_role(role, need, allowed) -> None:
    """A ``trigger``-typed actor is NOT internal automation that clears every
    floor (security review A-20): a fired run runs as the people who set the
    trigger up, so a trigger-typed ref (a legacy ownerless row) is ranked by
    the role it carries like any other actor, and a role-less one fails
    closed."""
    actor = PrincipalRef(
        type="trigger", id="trg-1", display="trg-1",
        role=role, source="internal",
    )
    assert _role_allows(actor, need) is allowed


def test_api_token_internal_actor_still_ranked_by_role() -> None:
    """An ``api_token`` actor is ``source == "internal"`` too, but must be
    ranked by the owner's real role -- NOT waved through like ``system``.
    Keying the always-allow branch on ``type`` (not ``source``) is what
    preserves this distinction."""
    token = PrincipalRef(
        type="api_token", id="t", display="t", role="user", source="internal",
    )
    assert _role_allows(token, "admin") is False   # ranked, not bypassed
    assert _role_allows(token, "user") is True


def test_principalref_projection_duck_types_like_principal() -> None:
    """The persisted PrincipalRef projection is accepted identically to a
    live Principal (the agent path passes ``initiated_by``, a PrincipalRef;
    the MCP path passes ``actor``, a Principal)."""
    ref = PrincipalRef(
        type="user", id="a", display="a", role="admin", source="local",
    )
    assert _role_allows(ref, "admin") is True


def test_role_rank_ordering() -> None:
    assert _ROLE_RANK["restricted"] < _ROLE_RANK["user"] < _ROLE_RANK["admin"]
