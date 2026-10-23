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

from primer.authz import _ROLE_RANK, _role_allows, principal_rank
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


# ---- principal_rank: how far a run reaches when it steers another session (pivot P3) ---------------------------------------------


def _ref(role, type_="user"):
    return PrincipalRef(type=type_, id="x", display="x", role=role, source="local")


def test_principal_rank_orders_system_admin_user_restricted():
    assert principal_rank(PrincipalRef.system()) == 3
    assert principal_rank(_ref("admin")) == 2
    assert principal_rank(_ref("user")) == 1
    assert principal_rank(_ref("restricted")) == 0
    assert principal_rank(_ref("auditor")) == -1
    assert principal_rank(None) == 1


def test_system_ranks_above_every_role():
    """A system run clears every role floor, so it outranks an admin; the rank is derived from the table, not a literal."""
    assert principal_rank(PrincipalRef.system()) == len(_ROLE_RANK) > max(_ROLE_RANK.values())


def test_a_run_nobody_can_name_ranks_as_an_ordinary_user():
    """No recorded initiator is the unattributed stand-in: a user, never the system (security review A-20)."""
    assert principal_rank(None) == principal_rank(PrincipalRef.unattributed()) == _ROLE_RANK["user"]


def test_an_unranked_role_loses_every_comparison():
    assert principal_rank(_ref(None)) == principal_rank(_ref("auditor")) == -1 < principal_rank(_ref("restricted"))


def test_a_live_principal_ranks_like_its_persisted_projection():
    assert principal_rank(_user("admin")) == principal_rank(_ref("admin")) == 2


def test_the_trigger_steer_rank_is_the_shared_rank():
    from primer.trigger.owner import _rank

    for ref in (None, PrincipalRef.system(), PrincipalRef.unattributed(), _ref("admin"), _ref("user", "trigger"), _ref(None, "api_token")):
        assert _rank(ref) == principal_rank(ref)


def test_the_trigger_steer_rank_goes_through_the_shared_rule(monkeypatch):
    """No second copy of the rule: the trigger's ``_rank`` calls ``principal_rank``, so changing the rule there changes both."""
    import primer.trigger.owner as owner

    monkeypatch.setattr(owner, "principal_rank", lambda ref: 42)
    assert owner._rank(_ref("user")) == 42


def test_may_drive_is_the_rank_rule_of_every_path_that_drives_a_session():
    """Lead rulings 2 and 3 of the G7 fixes: a person's or a token's steer and restart, a message from another session and a trigger's fire all
    ask ``may_drive``: the actor ranks at least as high as the session's creator. ``None`` ranks as an ordinary user on either side."""
    from primer.authz import may_drive

    def ref(role, type_="user"):
        return PrincipalRef(type=type_, id="x", display="x", role=role, source="local")

    assert may_drive(ref("admin"), ref("admin")) and may_drive(ref("admin"), ref("user")) and may_drive(ref("user"), ref("user"))
    assert not may_drive(ref("user"), ref("admin"))
    assert not may_drive(ref("admin"), PrincipalRef.system()) and may_drive(PrincipalRef.system(), ref("admin"))
    assert may_drive(None, ref("user")) and may_drive(ref("user"), None) and not may_drive(None, ref("admin"))
    assert not may_drive(ref("restricted"), ref("user")) and not may_drive(ref(None, "api_token"), ref("user"))


def test_the_own_session_rule_is_offered_only_where_asked_and_never_to_nobody():
    """Review of #853, N1: a live request may act on a session its own account or token started; a run (a peer message) is never offered it."""
    from primer.authz import may_drive, same_principal
    from primer.session.peer_messages import sender_may_message

    ada_then = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local")
    ada_now = ada_then.model_copy(update={"role": "user"})
    assert may_drive(ada_now, ada_then, own_session=True) and not may_drive(ada_now, ada_then)
    assert not sender_may_message(ada_now, ada_then), "a run with an admin's identity at user rank (a trigger-capped one) must not drive it"
    bob = PrincipalRef(type="user", id="u-bob", display="bob", role="user", source="local")
    assert not may_drive(bob, ada_then, own_session=True)
    token = PrincipalRef(type="api_token", id="u-ada", display="ci", role="user", source="internal")
    assert not same_principal(token, ada_then), "a token is not the account even when the ids collide"
    assert not same_principal(PrincipalRef.unattributed(), PrincipalRef.unattributed())
    assert not same_principal(PrincipalRef.system(), PrincipalRef.system()) and not same_principal(None, ada_then)
