"""PERF-T10 (M9) perf: the user refs of a row's owners and grants resolve in one query, not one ``get`` per id.

The view's answer is pinned by ``tests/access/test_use_grants.py`` (the view a grantee reads, the shape
admins get) and stays green through the change; the unknown-id half of this file pins the rest (an id
with no row resolves to a ref whose username is None, never an error). The perf half is RED on the N+1
(one ``users.get`` per owner/grantee id) and green once the ids resolve in one ``id IN`` find.
"""

from __future__ import annotations

from datetime import datetime, timezone

from primer.access.service import _user_refs
from primer.model.user import User
from tests.conftest import _FakeStorageProvider
from tests.perf.counting import CountingStorageProvider


def _user(uid: str, username: str) -> User:
    return User(id=uid, username=username, password_hash=None, created_at=datetime.now(timezone.utc))


async def test_user_refs_resolve_unknown_ids_to_a_none_username():
    sp = _FakeStorageProvider()
    users = sp.get_storage(User)
    await users.create(_user("u-ada", "ada"))
    refs = await _user_refs(users, ["u-ada", "u-gone", "u-ada"])
    assert refs["u-ada"].username == "ada"
    assert refs["u-gone"].username is None
    assert set(refs) == {"u-ada", "u-gone"}


async def test_user_refs_for_20_ids_makes_one_query():
    sp = _FakeStorageProvider()
    users = sp.get_storage(User)
    for i in range(10):
        await users.create(_user(f"u-{i}", f"user-{i}"))
    ids = [f"u-{i}" for i in range(10)] + [f"gone-{i}" for i in range(10)]

    csp = CountingStorageProvider(sp)
    with csp.counted() as c:
        refs = await _user_refs(csp.get_storage(User), ids)

    assert {uid: r.username for uid, r in refs.items()} == {f"u-{i}": f"user-{i}" for i in range(10)} | {f"gone-{i}": None for i in range(10)}
    # One IN find for the 20 ids (a single 200-row page), not one get per id.
    assert c.calls("get", "find", model="User") <= 2
