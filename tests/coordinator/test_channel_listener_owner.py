"""A writer on a separate PG bus wakes the elected API before its periodic refresh."""

import asyncio
from uuid import uuid4

from primer.api.registries.channel_registry import ChannelRegistry
from primer.bus.postgres import PostgresEventBus
from primer.channel.listener_owner import ChannelListenerOwner
from primer.channel.null_adapter import NullChannelAdapter
from primer.coordinator.postgres import PostgresInvalidationBus, PostgresLeaderElector
from primer.int.coordinator import InvalidationTopic
from primer.model.channel import Channel, ChannelProvider
from tests.pg_gate import postgres_marks

pytestmark = postgres_marks("Postgres channel listener ownership and invalidation")


async def test_postgres_writer_activates_room_only_on_listener_lease_holder(postgres_storage_provider, monkeypatch):
    sp = postgres_storage_provider
    actors = [PostgresLeaderElector(sp, owner_id=uuid4().hex) for _ in range(2)]
    reader, writer = PostgresEventBus(sp), PostgresEventBus(sp)
    providers, rooms = sp.get_storage(ChannelProvider), sp.get_storage(Channel)
    registry = ChannelRegistry(channel_storage=rooms, channel_provider_storage=providers,
                               storage_provider=sp, inbox=None, owns_inbound=False)
    owner = ChannelListenerOwner(registry)
    provider = ChannelProvider(id=uuid4().hex, provider="slack",
                               config={"app_token": "xapp-private", "bot_token": "xoxb-private"})

    def room():
        return Channel(id=uuid4().hex, provider="slack", provider_id=provider.id,
                       external_id=uuid4().hex, config={"chats": {"enabled": True}})

    first, second = room(), room()
    warmed = {first.id: asyncio.Event(), second.id: asyncio.Event()}

    async def build(_provider, channel, _inbox, *, owns_inbound, **kwargs):
        if channel.id in warmed:
            assert owns_inbound
            warmed[channel.id].set()
        return NullChannelAdapter()

    monkeypatch.setattr("primer.api.registries.channel_registry.build_adapter", build)
    await registry.bind_invalidation_bus(PostgresInvalidationBus(reader))
    probe = reader.subscribe(key="unused")
    try:
        await probe.ready()
        await providers.create(provider)
        await rooms.create(first)
        owner.start(actors[0])
        await asyncio.wait_for(warmed[first.id].wait(), 5)
        assert await actors[1].try_acquire(owner.role) is None
        await rooms.create(second)
        await PostgresInvalidationBus(writer).publish(InvalidationTopic.CHANNEL, second.id)
        await asyncio.wait_for(warmed[second.id].wait(), 5)
        await owner.stop()
        assert registry.peek_adapter(second.id) is None
        successor = await actors[1].try_acquire(owner.role)
        assert successor is not None
        await successor.release()
    finally:
        await owner.stop()
        await registry.aclose()
        await probe.aclose()
        await reader.aclose()
        await writer.aclose()
        for row in (first, second):
            if await rooms.get(row.id) is not None:
                await rooms.delete(row.id)
        if await providers.get(provider.id) is not None:
            await providers.delete(provider.id)
