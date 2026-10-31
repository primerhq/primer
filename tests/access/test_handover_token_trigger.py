"""The deletion hand-over disables the triggers a user's API tokens saved, not only the ones saved by the user themself (final review F16)."""
from __future__ import annotations

from datetime import UTC, datetime

from primer.access.lifecycle import hand_over
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.trigger import DelayedTriggerConfig, Trigger
from primer.model.user import User

NOW = datetime(2026, 10, 31, tzinfo=UTC)


async def test_a_trigger_saved_over_the_users_token_is_disabled_too(fake_storage_provider):
    sp = fake_storage_provider
    for uid, role in (("u-x", "user"), ("u-adm", "admin")):
        await sp.get_storage(User).create(User(id=uid, username=uid, created_at=NOW, role=role))
    tok_ref = PrincipalRef(type="api_token", id="tok-1", display="ci token", role="user", source="internal", user_id="u-x")
    usr_ref = PrincipalRef(type="user", id="u-x", display="x", role="user", source="local", user_id="u-x")
    other_ref = PrincipalRef(type="api_token", id="tok-2", display="other", role="user", source="internal", user_id="u-adm")
    for tid, ref in (("tr-tok", tok_ref), ("tr-usr", usr_ref), ("tr-other", other_ref)):
        await sp.get_storage(Trigger).create(Trigger(
            id=tid, slug=tid, name=tid, created_at=NOW, owner=ref, enabled=True, config=DelayedTriggerConfig(fire_at=NOW), access=Access(owners=["u-x"]),
        ))
    summary = await hand_over(sp, "u-x", "u-adm")
    enabled = {tid: (await sp.get_storage(Trigger).get(tid)).enabled for tid in ("tr-tok", "tr-usr", "tr-other")}
    assert enabled == {"tr-tok": False, "tr-usr": False, "tr-other": True}
    assert summary["Trigger.disabled"] == 2
