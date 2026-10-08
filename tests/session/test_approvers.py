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


# ---- a call_tool park written before the stamp existed (follow-up of #536) -----------------------------------------------------------
#
# Before the stamp, the `call_tool` park wrote no `approvers` KEY at all, and "no key" reads as anyone: a gate parked before the upgrade
# stays open to every user until it times out. Its metadata is recognisable (`via_call_tool` is written only by that park), so a call_tool
# park with the key ABSENT is decided by an admin only. Agent-loop parks have written the key since P6, so an absent key there is a
# park from before routing existed and stays open; an EXPLICIT None is "anyone" for both.

LEGACY_CALL_TOOL_PARK = {"via_call_tool": {"toolset_id": "system", "principal": None}, "original_call": {"id": "tc", "name": "x", "arguments": {}}}


def test_a_call_tool_park_without_the_approvers_key_is_decided_by_an_admin_only():
    assert may_decide(LEGACY_CALL_TOOL_PARK, username="root", role="admin")
    assert not may_decide(LEGACY_CALL_TOOL_PARK, username="bob", role="user")
    assert not may_decide(LEGACY_CALL_TOOL_PARK, username=None, role=None)


def test_a_call_tool_park_that_stamped_none_explicitly_is_decided_by_anyone():
    stamped = {**LEGACY_CALL_TOOL_PARK, "approvers": None}

    assert may_decide(stamped, username="bob", role="user")
    assert may_decide(stamped, username=None, role=None)


def test_an_agent_loop_park_without_the_key_stays_open():
    """No `via_call_tool`: a park from before approver routing existed, which has no restriction to honour."""
    legacy = {"original_call": {"id": "tc", "name": "x", "arguments": {}}}

    assert may_decide(legacy, username="bob", role="user")
