"""Exercise independent registry caches with real curated tools and private rooms."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from primer.api.registries.channel_registry import ChannelRegistry
from primer.channel.listener_owner import ChannelListenerOwner
from primer.channel.null_adapter import NullChannelAdapter
from primer.coordinator.in_memory import InMemoryInvalidationBus
from primer.int.coordinator import InvalidationTopic
from primer.model.channel import Channel, ChannelProvider
from primer.toolset.system import build_system_toolset
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401

PROVIDER = {"id": "cp", "provider": "slack", "config": {"app_token": "xapp-original", "bot_token": "xoxb-original"}}
ROOM = {"id": "room", "provider": "slack", "provider_id": "cp", "external_id": "C1", "config": {"chats": {"enabled": True}}}


def registry(storage, inbound=False):
    return ChannelRegistry(channel_storage=storage.get_storage(Channel), channel_provider_storage=storage.get_storage(ChannelProvider),
                           storage_provider=storage, inbox=None, owns_inbound=inbound)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.fixture
def builds(monkeypatch):
    built = []

    async def build(provider, channel, inbox, *, owns_inbound, **kwargs):
        adapter = NullChannelAdapter()
        adapter.owns_inbound = owns_inbound
        built.append((channel.id, provider.config.bot_token, adapter))
        return adapter

    monkeypatch.setattr("primer.api.registries.channel_registry.build_adapter", build)
    return built


async def test_worker_tool_writes_reach_owner_and_drop_stale_outbound_without_duplicate_starts(world, builds):
    storage, _, _ = world
    owner, worker = registry(storage, True), registry(storage)
    tools = build_system_toolset(storage_provider=storage, provider_registry=None, channel_registry=worker)
    bus = InMemoryInvalidationBus()
    await owner.bind_invalidation_bus(bus)
    await worker.bind_invalidation_bus(bus)
    tasks = [asyncio.create_task(owner.watch(inbound=True)), asyncio.create_task(worker.watch(inbound=False))]
    try:
        assert not (await _call(tools, "create_channel_provider", entity=PROVIDER))[0]
        assert not (await _call(tools, "create_channel", entity=ROOM))[0]
        await until(lambda: owner.peek_adapter("room"))
        first = owner.peek_adapter("room")
        assert first.owns_inbound and worker.peek_adapter("room") is None
        outbound = await worker.get_adapter("room")
        assert not outbound.owns_inbound
        assert not (await _call(tools, "update_channel_provider", id="cp", entity={
            **PROVIDER, "config": {**PROVIDER["config"], "bot_token": "xoxb-rotated"},
        }))[0]
        await until(lambda: first._closed and outbound._closed
                    and worker.peek_adapter("room") is None
                    and owner.peek_adapter("room") is not None
                    and owner.peek_adapter("room") is not first
                    and not owner.peek_adapter("room")._closed)
        second = owner.peek_adapter("room")
        assert second.owns_inbound and worker.peek_adapter("room") is None
        second_builds = [token for _, token, adapter in builds if adapter is second]
        assert len(second_builds) == 1
        assert second_builds[0].get_secret_value() == "xoxb-rotated"
        assert len(builds) == 3  # original owner, original outbound, rotated owner
        await owner.reconcile()
        assert owner.peek_adapter("room") is second
        assert len(builds) == 3
        assert not (await _call(tools, "update_channel", id="room", entity={**ROOM, "config": {"chats": {"enabled": False}}}))[0]
        await until(lambda: second._closed and owner.peek_adapter("room") is None)
        assert owner.peek_adapter("room") is None
        parked = await owner.get_adapter("room")
        assert not (await _call(tools, "delete_channel", id="room"))[0]
        await until(lambda: parked._closed)
        await bus.publish(InvalidationTopic.CHANNEL, "room")  # delayed update hint after deletion
        await owner.reconcile()
        assert owner.peek_adapter("room") is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await worker.aclose()
        await owner.aclose()


async def test_rotation_does_not_complete_at_closed_but_still_cached_adapter(world, builds, monkeypatch):
    storage, _, _ = world
    providers = storage.get_storage(ChannelProvider)
    await providers.create(ChannelProvider(**PROVIDER))
    await storage.get_storage(Channel).create(Channel(**ROOM))
    owner = registry(storage, True)
    await owner.reconcile()
    first = owner.peek_adapter("room")
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = first.aclose

    async def closing():
        await original_close()
        entered.set()
        await release.wait()  # deterministic closed-before-cache-removal interval

    monkeypatch.setattr(first, 'aclose', closing)
    await providers.update(ChannelProvider(**{
        **PROVIDER, "config": {**PROVIDER["config"], "bot_token": "xoxb-rotated"},
    }))
    rotation = asyncio.create_task(owner.reconcile())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # The old completion predicate accepted this state even though no new
        # adapter existed. Real get_adapter callers wait behind the registry lock.
        assert first._closed and owner.peek_adapter("room") is first
        assert not rotation.done() and len(builds) == 1
        release.set()
        assert await asyncio.wait_for(rotation, 3) == 1
        second = owner.peek_adapter("room")
        assert second is not first and not second._closed and second.owns_inbound
        assert [(token.get_secret_value(), adapter is second) for _, token, adapter in builds] == [
            ('xoxb-original', False), ('xoxb-rotated', True),
        ]
        assert await owner.reconcile() == 0
        assert owner.peek_adapter("room") is second and len(builds) == 2
    finally:
        release.set()
        if not rotation.done():
            rotation.cancel()
        await asyncio.gather(rotation, return_exceptions=True)
        await owner.aclose()


async def test_cold_scan_recovers_missed_create_and_retries_failures_without_hot_loop(world, monkeypatch):
    storage, _, _ = world
    await storage.get_storage(ChannelProvider).create(ChannelProvider(**PROVIDER))
    rows = storage.get_storage(Channel)
    await rows.create(Channel(**ROOM))
    await rows.create(Channel(**{**ROOM, "id": "second", "external_id": "C2"}))
    reg = registry(storage, True)
    attempts = []

    async def build(provider, channel, *args, **kwargs):
        attempts.append(channel.id)
        if channel.id == "room" and attempts.count("room") == 1:
            raise ConnectionError("sensitive SDK detail")
        return NullChannelAdapter()

    monkeypatch.setattr("primer.api.registries.channel_registry.build_adapter", build)
    try:
        assert await reg.reconcile() == 1
        assert reg.peek_adapter("second") and reg.peek_adapter("room") is None
        assert await reg.reconcile() == 0
        assert attempts.count("room") == 1
        # Advance only the retry deadline; the production scan retries the unchanged failed config.
        room, provider, _, delay = reg._retry["room"]
        reg._retry["room"] = (room, provider, 0, delay)
        assert await reg.reconcile() == 1
        assert attempts.count("room") == 2 and reg.peek_adapter("room")
    finally:
        await reg.aclose()


async def test_election_loss_closes_gateway_before_release_and_follower_promotion(world, builds):
    storage, _, _ = world
    await storage.get_storage(ChannelProvider).create(ChannelProvider(**PROVIDER))
    await storage.get_storage(Channel).create(Channel(**ROOM))
    first, second = registry(storage), registry(storage)
    follower = await second.get_adapter("room")
    first_task, second_task = ChannelListenerOwner(first), ChannelListenerOwner(second)
    released = asyncio.Event()
    lease = SimpleNamespace(lost_event=asyncio.Event())

    async def release():
        assert first.peek_adapter("room") is None and not first.owns_inbound
        released.set()

    lease.release = release
    elector = SimpleNamespace(try_acquire=AsyncMock(side_effect=[lease, None]))

    async def elect_peer(role):
        assert role == "channel-listeners"
        await released.wait()
        return SimpleNamespace(lost_event=asyncio.Event(), release=AsyncMock())

    first_task.start(elector)
    second_task.start(SimpleNamespace(try_acquire=elect_peer))
    try:
        await until(lambda: first.peek_adapter("room"))
        listener = first.peek_adapter("room")
        assert listener.owns_inbound and not follower.owns_inbound and not follower._closed
        lease.lost_event.set()
        await until(lambda: second.peek_adapter("room") and second.peek_adapter("room").owns_inbound)
        assert listener._closed and follower._closed
        assert not (await first.get_adapter("room")).owns_inbound
    finally:
        await first_task.stop()
        await second_task.stop()
        await first.aclose()
        await second.aclose()


async def test_repeated_cancellation_drains_the_same_sdk_release(world, builds):
    storage, _, _ = world
    await storage.get_storage(ChannelProvider).create(ChannelProvider(**PROVIDER))
    await storage.get_storage(Channel).create(Channel(**ROOM))
    reg = registry(storage, True)
    client = await reg.get_adapter("room")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await finish.wait()

    client.aclose = AsyncMock(side_effect=close)
    closing = asyncio.create_task(reg.set_inbound(False))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done() and reg.peek_adapter("room") is None
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
    await reg.reconcile()
    client.aclose.assert_awaited_once()
    assert not (await reg.get_adapter("room")).owns_inbound
    await reg.aclose()


async def test_failed_demotion_refuses_cached_client_until_drain_succeeds(world, builds):
    storage, _, _ = world
    await storage.get_storage(ChannelProvider).create(ChannelProvider(**PROVIDER))
    await storage.get_storage(Channel).create(Channel(**ROOM))
    reg = registry(storage, True)
    old = await reg.get_adapter("room")
    old.aclose = AsyncMock(side_effect=[ConnectionError(), None])
    with pytest.raises(ConnectionError):
        await reg.set_inbound(False)
    assert not reg.owns_inbound and reg.peek_adapter("room") is None
    with pytest.raises(RuntimeError, match="draining"):
        await reg.get_adapter("room")
    await reg.reconcile()
    new = await reg.get_adapter("room")
    assert new is not old and not new.owns_inbound and old.aclose.await_count == 2
    await reg.aclose()


async def test_slack_failed_connect_closes_unpublished_client(monkeypatch):
    from primer.channel.slack import connection

    conn = SimpleNamespace(start_async=AsyncMock(side_effect=OSError()), close_async=AsyncMock())
    monkeypatch.setattr(connection, "_build_app_and_handler", lambda cfg: conn)
    pool = connection._SlackConnectionRegistry()
    with pytest.raises(OSError):
        await pool.acquire(ChannelProvider(**PROVIDER))
    conn.close_async.assert_awaited_once()
    assert not pool._entries


async def test_telegram_cancelled_poll_start_stops_partial_application(monkeypatch):
    from primer.channel.telegram import connection

    updater = SimpleNamespace(running=True, start_polling=AsyncMock(side_effect=asyncio.CancelledError), stop=AsyncMock())
    app = SimpleNamespace(initialize=AsyncMock(), start=AsyncMock(), running=True, updater=updater,
                          stop=AsyncMock(), shutdown=AsyncMock())
    monkeypatch.setattr(connection, "_build_application", lambda cfg: app)
    pool = connection._TelegramConnectionRegistry()
    provider = ChannelProvider(id="tg", provider="telegram", config={"bot_token": "123456:offline-placeholder"})
    with pytest.raises(asyncio.CancelledError):
        await pool.acquire(provider)
    for method in (updater.stop, app.stop, app.shutdown):
        method.assert_awaited_once()
    assert not pool._entries


async def test_supervisor_repeated_stop_waits_for_all_providers_and_retry(world, builds, monkeypatch):
    from primer.channel import retirement

    monkeypatch.setattr(retirement, "_RETRY_SECONDS", .01)
    sp, _, _ = world
    for i in range(2):
        await sp.get_storage(ChannelProvider).create(ChannelProvider(**{**PROVIDER, "id": f"cp-{i}"}))
        await sp.get_storage(Channel).create(Channel(**{**ROOM, "id": f"room-{i}", "provider_id": f"cp-{i}"}))
    reg = registry(sp)
    owner = ChannelListenerOwner(reg)
    lease = SimpleNamespace(lost_event=asyncio.Event())
    released = asyncio.Event()
    done = set()
    entered = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]

    async def release():
        assert done == {0, 1}
        released.set()

    lease.release = release
    owner.start(SimpleNamespace(try_acquire=AsyncMock(side_effect=[lease, None])))
    try:
        await until(lambda: len(builds) == 2)
        for i in range(2):
            async def close(i=i):
                if i == 1 and not entered[i].is_set():
                    entered[i].set()
                    raise ConnectionError("transient SDK failure")
                entered[i].set()
                await finish[i].wait()
                done.add(i)
            reg.peek_adapter(f"room-{i}").aclose = close
        lease.lost_event.set()
        await asyncio.wait_for(entered[0].wait(), 2)
        await asyncio.wait_for(entered[1].wait(), 2)
        stopping = asyncio.create_task(owner.stop())
        await asyncio.sleep(0)
        owner._task.cancel()
        await asyncio.sleep(0)
        finish[0].set()
        await until(lambda: 0 in done)
        assert not released.is_set() and not stopping.done()
        finish[1].set()
        await asyncio.wait_for(stopping, 2)
        assert released.is_set() and reg.peek_adapter("room-0") is reg.peek_adapter("room-1") is None
    finally:
        for gate in finish:
            gate.set()
        await owner.stop()
        await reg.aclose()


@pytest.mark.parametrize("legacy_standing", [False, True], ids=["current", "legacy"])
async def test_disabled_rooms_use_standing_or_audible_session_binding_without_a_correlation(world, builds, legacy_standing):
    from datetime import UTC, datetime
    from primer.model.workspace import Workspace, WorkspaceChannelLink, WorkspaceRuntimeMeta
    from primer.model.workspace_session import WorkspaceSession
    from tests.api.test_gate_fence import G1, _ask_user_session

    sp, _, _ = world
    await sp.get_storage(ChannelProvider).create(ChannelProvider(**PROVIDER))
    for name in ("standing", "ephemeral", "silent", "unused"):
        await sp.get_storage(Channel).create(Channel(id=name, external_id=name, provider="slack", provider_id="cp"))
    await sp.get_storage(Workspace).create(Workspace(
        id="w", provider_id="local", template_id="t", created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://unused", token="fixture"), reply_binding=WorkspaceChannelLink(channel_id="standing"),
    ))
    if legacy_standing:
        # Persist the historical spelling so this covers the storage query as well as model reads.
        await sp.connection.execute(
            """UPDATE workspace SET data = json_remove(
                   json_set(data, '$.channel_association', json_extract(data, '$.reply_binding')), '$.reply_binding'
               ) WHERE id = ?""", ("w",),
        )
        await sp.connection.commit()
        assert (await sp.get_storage(Workspace).get("w")).reply_binding.channel_id == "standing"
    for name in ("ephemeral", "silent"):
        session = _ask_user_session(session_id=name, tool_call_id="ask", gate_id=G1)
        session.metadata["reply_binding"] = {"channel_id": name}
        if name == "silent":
            session.metadata["reply_binding"]["silent"] = True
        await sp.get_storage(WorkspaceSession).create(session)
    reg = registry(sp, True)
    try:
        assert await reg.reconcile() == 2
        assert {cid for cid, *_ in builds} == {"standing", "ephemeral"}
    finally:
        await reg.aclose()
