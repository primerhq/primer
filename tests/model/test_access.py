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


def test_public_tools_make_it_publicly_listable():
    assert "public" in Access(owners=["u1"], public_tools=["t"]).members


def test_owned_by():
    assert Access.owned_by("u1").owners == ["u1"] and Access.owned_by("u1").public is False
