"""Fire-time and delivery checks judge the role an owner holds NOW, capped by the recorded one (final review F4)."""
from __future__ import annotations

from primer.model.access import Access
from primer.model.trigger import Trigger
from primer.model.user import User
from tests.trigger.test_steer_rank_guard import APPEND, _append, _ref, _seed


async def _set_user(sp, uid, **fields):
    u = await sp.get_storage(User).get(uid)
    await sp.get_storage(User).update(u.model_copy(update=fields))


async def test_a_demoted_admin_no_longer_fires_on_a_private_trigger_they_cannot_read(monkeypatch, fake_storage_provider):
    sp = fake_storage_provider
    sub = await _seed(sp, session_by=_ref("u-admin", "user"), owner=_ref("u-admin", "admin"), config=APPEND, trigger_owner=_ref("u-plain", "user"))
    tr = await sp.get_storage(Trigger).get("tr-1")
    await sp.get_storage(Trigger).update(tr.model_copy(update={"access": Access(owners=["u-plain"])}))  # private to u-plain
    await _set_user(sp, "u-admin", role="user")
    res, delivered = await _append(monkeypatch, sp, sub)
    assert delivered == [] and not res.ok
