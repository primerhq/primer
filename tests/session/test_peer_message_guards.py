"""The guards of a peer message (pivot P3, plan tasks 4 and 5): the rank rule, the hop limit, the rate ledger, the scope and the display helpers.

``message_session`` is the one way an agent drives another session's run, so what it may do is decided here, before anything is written. The rank
rule is the one a trigger's steer uses (``primer.authz.principal_rank``); the tests in this file pin how the peer guards read it.
"""

from __future__ import annotations

import pytest

from primer.model.principal import PrincipalRef
from primer.session.peer_messages import PeerMessageRefused, rank_label, sender_may_message


def _ref(role, type_="user"):
    return PrincipalRef(type=type_, id="x", display="x", role=role, source="local")


# ---- the rank rule (task 4) -------------------------------------------------------------------------------------------------------


def test_a_user_run_cannot_message_a_session_an_admin_started():
    user, admin = _ref("user"), _ref("admin")
    assert sender_may_message(user, admin) is False
    assert sender_may_message(admin, user) is True
    assert sender_may_message(user, user) is True
    assert sender_may_message(user, None) is True            # no recorded creator: it runs as an unattributed user
    assert sender_may_message(None, user) is False           # an unknown sender fails closed
    assert sender_may_message(PrincipalRef.system(), admin) is True
    assert sender_may_message(admin, PrincipalRef.system()) is False


@pytest.mark.parametrize(
    ("sender", "target", "allowed"),
    [
        pytest.param(_ref("restricted"), _ref("user"), False, id="restricted to user"),
        pytest.param(_ref("restricted"), _ref("restricted"), True, id="restricted to restricted"),
        pytest.param(_ref("user"), _ref("restricted"), True, id="user to restricted"),
        pytest.param(PrincipalRef.system(), PrincipalRef.system(), True, id="system to system"),
        pytest.param(PrincipalRef.unattributed(), None, True, id="unattributed to no creator"),
        pytest.param(PrincipalRef.unattributed(), _ref("admin"), False, id="unattributed to admin"),
        pytest.param(_ref(None), _ref("user"), False, id="a sender with no role to user"),
        pytest.param(_ref("auditor"), _ref("user"), False, id="an unranked role to user"),
        pytest.param(_ref("restricted"), _ref(None, "trigger"), True, id="restricted to a trigger run with no role"),
        pytest.param(_ref("user", "api_token"), _ref("user"), True, id="a token run ranks by its role"),
    ],
)
def test_the_sender_must_rank_at_least_as_high_as_the_target_creator(sender, target, allowed):
    assert sender_may_message(sender, target) is allowed


def test_the_refusal_carries_its_type_and_text():
    exc = PeerMessageRefused("forbidden", "no")
    assert (exc.error_type, exc.message, str(exc)) == ("forbidden", "no", "no")
    assert rank_label(None) == "user (unattributed)"
    assert rank_label(PrincipalRef.system()) == "system"


def test_the_rank_label_names_the_role_and_never_who_it_is():
    """The refusal text reaches the sending agent: it says at which rank the target runs, not which person or token started it."""
    assert rank_label(_ref("admin")) == "admin"
    assert rank_label(_ref("restricted", "api_token")) == "restricted"
