"""The Access value and the Owned mixin (access-control spec 6.1).

``members`` is derived from the other fields and never trusted from a
client body; a grant for a user who is also an owner is dropped.
"""

from primer.model.access import Access


def test_members_are_derived_and_client_values_ignored():
    a = Access(owners=["u1"], grants={"u2": "read"}, public=True, members=["u:evil"])
    assert a.members == ["public", "u:u1", "u:u2"]


def test_owner_grant_is_dropped():
    assert Access(owners=["u1"], grants={"u1": "read"}).grants == {}


def test_public_tools_do_not_make_the_row_listable():
    # Per-tool public (Phase 4's tool_is_public) must not leak the row itself
    # into every user's list: only the row-level public flag adds the token.
    assert Access(owners=["u1"], public_tools=["t"]).members == ["u:u1"]


def test_owned_by():
    assert Access.owned_by("u1").owners == ["u1"] and Access.owned_by("u1").public is False


def test_rev_defaults_to_zero_and_is_not_a_member():
    a = Access(owners=["u1"], rev=3)
    assert Access().rev == 0 and a.rev == 3 and a.members == ["u:u1"]
