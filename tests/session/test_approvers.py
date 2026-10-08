"""The shared approver enforcement (ticket 01a11b64): who may decide a gate, for every surface that answers one."""

from __future__ import annotations

import logging

import pytest

from primer.model.except_ import PrimerError
from primer.session.approvers import ApproverRefusedError, ensure_may_decide, may_decide

ALICE_ONLY = {"approvers": {"kind": "users", "roles": [], "users": ["alice"]}}
OPS_ONLY = {"approvers": {"kind": "roles", "roles": ["ops"], "users": []}}
ADMIN_ONLY = {"approvers": {"kind": "roles", "roles": [], "users": []}}


@pytest.mark.parametrize("metadata", [None, {}, {"approvers": None}, {"approvers": {}}, {"approvers": {"kind": "anyone"}}])
def test_a_gate_without_a_restriction_is_decided_by_anyone_identified_or_not(metadata):
    assert may_decide(metadata, username="bob", role="user")
    assert may_decide(metadata, username=None, role=None), "a chat-platform user may answer an unrestricted gate"


def test_a_users_spec_admits_the_named_user_and_an_admin_only():
    assert may_decide(ALICE_ONLY, username="alice", role="user")
    assert may_decide(ALICE_ONLY, username="root", role="admin")
    assert not may_decide(ALICE_ONLY, username="bob", role="user")


def test_a_roles_spec_admits_the_role_and_an_admin_only():
    assert may_decide(OPS_ONLY, username="olive", role="ops")
    assert may_decide(OPS_ONLY, username="root", role="admin")
    assert not may_decide(OPS_ONLY, username="bob", role="user")


def test_an_admin_only_spec_admits_an_admin_and_nobody_else():
    assert may_decide(ADMIN_ONLY, username="root", role="admin")
    assert not may_decide(ADMIN_ONLY, username="alice", role="user")


@pytest.mark.parametrize("metadata", [ALICE_ONLY, OPS_ONLY, ADMIN_ONLY], ids=["users", "roles", "admin-only"])
def test_an_unidentified_decider_never_decides_a_restricted_gate(metadata):
    """A channel user's platform id maps to no primer account: it cannot be shown to be alice, an ops member or an admin."""
    assert not may_decide(metadata, username=None, role=None)
    assert not may_decide(metadata, username="alice", role=None)
    assert not may_decide(metadata, username=None, role="admin")


def test_a_stored_spec_that_cannot_be_read_fails_closed_to_admin_only(caplog):
    broken = {"approvers": {"kind": "bogus"}}

    with caplog.at_level(logging.WARNING, logger="primer.session.approvers"):
        assert not may_decide(broken, username="bob", role="user")
        assert may_decide(broken, username="root", role="admin")

    assert any("unreadable stored approvers" in r.getMessage() for r in caplog.records)


def test_ensure_may_decide_raises_for_a_refused_decider_and_is_silent_for_an_admitted_one():
    ensure_may_decide(ALICE_ONLY, username="alice", role="user")
    ensure_may_decide(None, username=None, role=None)

    with pytest.raises(ApproverRefusedError) as caught:
        ensure_may_decide(ALICE_ONLY, username="bob", role="user")

    assert isinstance(caught.value, PrimerError)
    assert "approver" in str(caught.value)
