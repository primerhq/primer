"""A channel provider is USED when a channel is saved on it and when a channel on it is bound (access-control P4 Task 6, plan Decision 5).

The saver of a channel must be able to use its provider (an unusable one reads like a missing one, a provider the channel already names is not
re-checked on an edit), and so must the binder of a channel (a workspace's reply binding, a channel trigger): a channel on an unusable provider
reads like a missing channel. Admins use any.
"""

from __future__ import annotations

import pytest

from primer.access.person import Person
from primer.channel.checks import channel_for_binding, check_channel_on_create, check_channel_on_update
from primer.common.entity_checks import EntityCheckError
from primer.model.access import Access
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import NotFoundError
from primer.model.trigger import ChannelTriggerConfig
from primer.trigger.channel_access import require_channel_trigger_access

ADA, ROOT = Person("ada", "user"), Person("root", "admin")


async def _providers(sp) -> None:
    for pid, access in (("cp-pub", Access(public=True)), ("cp-priv", Access()), ("cp-ada", Access(grants={"ada": "read"}, rev=1))):
        await sp.get_storage(ChannelProvider).create(
            ChannelProvider(id=pid, provider="slack", config={"app_token": "xapp-test", "bot_token": "xoxb-test"}, access=access)
        )


def _channel(cid: str, provider_id: str, external: str = "C1") -> Channel:
    return Channel(id=cid, provider_id=provider_id, provider="slack", external_id=external, access=Access(public=True))


async def test_save_needs_provider_use(fake_storage_provider):
    sp = fake_storage_provider
    await _providers(sp)

    await check_channel_on_create(_channel("c1", "cp-pub", "C1"), storage_provider=sp, person=ADA)
    await check_channel_on_create(_channel("c2", "cp-ada", "C2"), storage_provider=sp, person=ADA)
    await check_channel_on_create(_channel("c3", "cp-priv", "C3"), storage_provider=sp, person=ROOT)
    await check_channel_on_create(_channel("c4", "cp-priv", "C4"), storage_provider=sp)  # internal callers are unchecked
    with pytest.raises(EntityCheckError, match="does not exist") as unusable:
        await check_channel_on_create(_channel("c5", "cp-priv", "C5"), storage_provider=sp, person=ADA)
    with pytest.raises(EntityCheckError, match="does not exist") as missing:
        await check_channel_on_create(_channel("c6", "cp-nope", "C6"), storage_provider=sp, person=ADA)
    assert unusable.value.message.replace("cp-priv", "X") == missing.value.message.replace("cp-nope", "X")


async def test_an_edit_asks_the_saver_only_about_a_provider_it_adds(fake_storage_provider):
    sp = fake_storage_provider
    await _providers(sp)
    existing = _channel("c1", "cp-priv")

    # The channel keeps its (private) provider: not re-checked.
    await check_channel_on_update(existing, storage_provider=sp, person=ADA, existing=existing)
    # Moving it onto one the saver may not use is refused; onto a usable one is fine.
    with pytest.raises(EntityCheckError, match="does not exist"):
        await check_channel_on_update(_channel("c1", "cp-priv", "C1"), storage_provider=sp, person=ADA, existing=_channel("c1", "cp-pub"))
    await check_channel_on_update(_channel("c1", "cp-ada", "C1"), storage_provider=sp, person=ADA, existing=_channel("c1", "cp-pub"))


async def test_a_channel_on_an_unusable_provider_cannot_be_bound(fake_storage_provider):
    sp = fake_storage_provider
    await _providers(sp)
    for cid, pid in (("ch-pub", "cp-pub"), ("ch-ada", "cp-ada"), ("ch-priv", "cp-priv")):
        await sp.get_storage(Channel).create(_channel(cid, pid, external=cid))

    assert (await channel_for_binding(sp, ADA, "ch-pub")).id == "ch-pub"
    assert (await channel_for_binding(sp, ADA, "ch-ada")).id == "ch-ada"
    assert await channel_for_binding(sp, ADA, "ch-priv") is None
    assert await channel_for_binding(sp, ADA, "ch-nope") is None
    assert (await channel_for_binding(sp, ROOT, "ch-priv")).id == "ch-priv"


async def test_a_channel_trigger_needs_the_channels_provider_too(fake_storage_provider):
    sp = fake_storage_provider
    await _providers(sp)
    await sp.get_storage(Channel).create(_channel("ch-priv", "cp-priv"))
    await sp.get_storage(Channel).create(_channel("ch-pub", "cp-pub", "C9"))

    await require_channel_trigger_access(sp, ADA, ChannelTriggerConfig(provider_id="slack-1", channel_id="ch-pub"))
    with pytest.raises(NotFoundError, match="does not exist"):
        await require_channel_trigger_access(sp, ADA, ChannelTriggerConfig(provider_id="slack-1", channel_id="ch-priv"))
    await require_channel_trigger_access(sp, ROOT, ChannelTriggerConfig(provider_id="slack-1", channel_id="ch-priv"))
