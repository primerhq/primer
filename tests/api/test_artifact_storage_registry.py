"""ArtifactStorageRegistry: cache, invalidate, default resolution."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from primer.api.registries.artifact_storage_registry import ArtifactStorageRegistry
from primer.artifact.routing import RoutingArtifactStorage
from primer.coordinator.in_memory import InMemoryInvalidationBus
from primer.int.coordinator import InvalidationTopic
from primer.model.except_ import NotFoundError
from tests.artifact._stores import MemStore
from tests.conftest import _FakeStorageProvider


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, entity_id):
        return self._rows.get(entity_id)


class _StubArtifactStorage:
    def __init__(self):
        self.closed = False
        self.initialized = False

    async def initialize(self):
        self.initialized = True

    def serves(self, row):
        # ``ArtifactStorage.serves``: the routing store asks it at the re-read after a put's record. A stub's location never moves.
        return True

    async def aclose(self):
        self.closed = True


def _factory_returning(instances):
    def _f(row, storage_provider):
        inst = _StubArtifactStorage()
        instances.append(inst)
        return inst
    return _f


@pytest.mark.asyncio
async def test_caches_instance_per_id():
    rows = {"asp-a": object()}
    instances = []
    reg = ArtifactStorageRegistry(
        storage=_FakeStore(rows), storage_provider=None,
        factory=_factory_returning(instances))
    a = await reg.get_provider("asp-a")
    b = await reg.get_provider("asp-a")
    assert a is b
    assert len(instances) == 1
    assert instances[0].initialized is True


@pytest.mark.asyncio
async def test_invalidate_closes_instance():
    """Re-pinned in slice D round 2: an eviction closes a store at once only when no call holds a lease on it, so the store is released first
    (a leased store is retired and closed when its last lease returns: the lease tests below)."""
    rows = {"asp-a": object()}
    instances = []
    reg = ArtifactStorageRegistry(
        storage=_FakeStore(rows), storage_provider=None,
        factory=_factory_returning(instances))
    await reg.release_provider(await reg.get_provider("asp-a"))
    await reg.invalidate("asp-a")
    assert instances[0].closed is True


@pytest.mark.asyncio
async def test_get_default_returns_one_routing_store():
    """Pinned the old design (a fixed-id read of the reserved row). ``get_default()`` now returns the routing store, built once: it reads the active
    setting and the location records per call, so it needs no provider row at construction and builds no provider instance."""
    instances = []
    reg = ArtifactStorageRegistry(
        storage=_FakeStore({}), storage_provider=_FakeStorageProvider(),
        factory=_factory_returning(instances))
    first, second = await reg.get_default(), await reg.get_default()
    assert isinstance(first, RoutingArtifactStorage) and first is second and instances == []


@pytest.mark.asyncio
async def test_the_routing_store_resolves_providers_through_the_registry():
    """A provider the active setting names is resolved by ``get_provider``, so it is built once and shared with every other reader of the cache."""
    from primer.model.artifact import ArtifactLocation
    from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig

    class _Store(_StubArtifactStorage):
        needs_location_record = True

        async def put(self, *, data, mime_type, filename=None):
            return "artifact-" + uuid4().hex

        async def delete(self, artifact_id):
            return None

    built = []

    def _factory(row, storage_provider):
        built.append(row)
        return _Store()

    sp = _FakeStorageProvider()
    await sp.get_storage(ActiveArtifactStorageConfig).create(ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="asp-a"))
    reg = ArtifactStorageRegistry(storage=_FakeStore({"asp-a": object()}), storage_provider=sp, factory=_factory)

    routing = await reg.get_default()
    aid = await routing.put(data=b"x", mime_type="text/plain", filename=None)
    await routing.put(data=b"y", mime_type="text/plain", filename=None)

    assert (await sp.get_storage(ArtifactLocation).get(aid)).provider_id == "asp-a"
    assert len(built) == 1, "two puts, one provider instance: the routing store shares the registry's cache"


@pytest.mark.asyncio
async def test_the_routing_store_checks_the_provider_row_not_the_cache_after_the_record():
    """A put confirms the provider row still exists after it writes the record, by reading the registry's row storage: the cached instance
    outlives a delete until the eviction lands (the bus publishes, every process evicts asynchronously), so ``resolve`` cannot tell (the G5
    audit, G5-03). Here the instance is cached and the row is gone: the put fails with a 409 and leaves no record."""
    from primer.model.artifact import ArtifactLocation
    from primer.model.except_ import ConflictError
    from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig
    from primer.model.storage import OffsetPage

    class _Store(_StubArtifactStorage):
        needs_location_record = True

        def __init__(self):
            super().__init__()
            self.deleted = []

        async def put(self, *, data, mime_type, filename=None):
            return "artifact-" + uuid4().hex

        async def delete(self, artifact_id):
            self.deleted.append(artifact_id)

    store = _Store()
    rows = {"asp-a": object()}
    sp = _FakeStorageProvider()
    await sp.get_storage(ActiveArtifactStorageConfig).create(ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="asp-a"))
    reg = ArtifactStorageRegistry(storage=_FakeStore(rows), storage_provider=sp, factory=lambda row, storage_provider: store)
    routing = await reg.get_default()
    await routing.put(data=b"x", mime_type="text/plain", filename=None)       # caches the instance

    del rows["asp-a"]                                                         # the row is deleted; the cached instance is not evicted yet
    with pytest.raises(ConflictError):
        await routing.put(data=b"y", mime_type="text/plain", filename=None)

    assert len((await sp.get_storage(ArtifactLocation).list(OffsetPage(offset=0, length=10))).items) == 1, "only the first put's record"
    assert len(store.deleted) == 1, "the second put's bytes were removed"


@pytest.mark.asyncio
async def test_the_routing_store_uses_the_registrys_default_provider_id():
    """The registry keeps its ``default_provider_id`` argument and hands it to the routing store as the id that means "the database"."""
    from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig

    sp = _FakeStorageProvider()
    await sp.get_storage(ActiveArtifactStorageConfig).create(ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="my-default"))
    reg = ArtifactStorageRegistry(
        storage=_FakeStore({}), storage_provider=sp, default_provider_id="my-default", factory=_factory_returning([]))

    routing = await reg.get_default()
    aid = await routing.put(data=b"x", mime_type="text/plain", filename=None)

    from primer.model.artifact import Artifact

    assert await sp.get_storage(Artifact).get(aid) is not None, "'my-default' is the database: no registry lookup, no record"


@pytest.mark.asyncio
async def test_unknown_id_raises():
    reg = ArtifactStorageRegistry(
        storage=_FakeStore({}), storage_provider=None,
        factory=_factory_returning([]))
    with pytest.raises(NotFoundError):
        await reg.get_provider("asp-missing")


# ---- the invalidation bus (plan 09, Task 3; the artifact half of ticket 01a11358) ----------------------------------------------------------------
#
# The registry caches one live store per provider row in EACH process. A REST edit used to clear only the cache of the process that served it, and a
# system tool (which runs in whichever process executes the session, a worker in the k3s split) cleared nothing. The registry now publishes on the
# ``artifact_storage_provider`` topic and every process, the publisher included, evicts its own copy from its subscription.


def _registry(rows=None, instances=None):
    return ArtifactStorageRegistry(
        storage=_FakeStore({"asp-a": object()} if rows is None else rows), storage_provider=None,
        factory=_factory_returning([] if instances is None else instances))


class _CountingBus(InMemoryInvalidationBus):
    """An in-memory bus that counts the subscriptions made on it, and the events published."""

    def __init__(self):
        super().__init__()
        self.subscribed = []
        self.published = []

    async def subscribe(self, topic, handler, *, on_reconnect=None):
        self.subscribed.append((topic, on_reconnect))
        return await super().subscribe(topic, handler, on_reconnect=on_reconnect)

    async def publish(self, topic, key):
        self.published.append((topic, key))
        await super().publish(topic, key)


class _FailingSubscribeBus(_CountingBus):
    async def subscribe(self, topic, handler, *, on_reconnect=None):
        self.subscribed.append((topic, on_reconnect))
        raise RuntimeError("subscribe failed")


@pytest.mark.asyncio
async def test_an_invalidation_reaches_every_registry_on_the_bus():
    bus = InMemoryInvalidationBus()
    api, worker = _registry(), _registry()                 # two processes' registries
    await api.bind_invalidation_bus(bus)
    await worker.bind_invalidation_bus(bus)
    a, w = await api.get_provider("asp-a"), await worker.get_provider("asp-a")
    await api.release_provider(a)
    await worker.release_provider(w)                       # idle in both processes, so the eviction closes both at once

    await api.invalidate("asp-a")

    assert a.closed and w.closed
    assert await worker.get_provider("asp-a") is not w
    assert await api.get_provider("asp-a") is not a


async def test_a_failed_subscribe_leaves_the_registry_unbound() -> None:
    """The #784 review pin: a bind that cannot subscribe must not strand later invalidations on a bus this registry never hears."""
    reg = _registry()
    cached = await reg.get_provider("asp-a")
    await reg.release_provider(cached)

    with pytest.raises(RuntimeError, match="subscribe failed"):
        await reg.bind_invalidation_bus(_FailingSubscribeBus())

    await reg.invalidate("asp-a")

    assert cached.closed
    assert await reg.get_provider("asp-a") is not cached


@pytest.mark.asyncio
async def test_an_invalidation_from_either_side_reaches_the_other():
    bus = InMemoryInvalidationBus()
    api, worker = _registry(), _registry()
    await api.bind_invalidation_bus(bus)
    await worker.bind_invalidation_bus(bus)
    a = await api.get_provider("asp-a")
    await api.release_provider(a)

    await worker.invalidate("asp-a")                       # a tool edit in a worker process

    assert a.closed, "the API process's cached store was dropped by an edit made elsewhere"


@pytest.mark.asyncio
async def test_with_a_bus_bound_invalidate_publishes_on_the_artifact_topic():
    bus = _CountingBus()
    reg = _registry()
    await reg.bind_invalidation_bus(bus)

    await reg.invalidate("asp-a")

    assert bus.published == [(InvalidationTopic.ARTIFACT_STORAGE_PROVIDER, "asp-a")]


@pytest.mark.asyncio
async def test_an_invalidation_for_another_provider_leaves_the_cache_alone():
    bus = InMemoryInvalidationBus()
    reg = _registry({"asp-a": object(), "asp-b": object()})
    await reg.bind_invalidation_bus(bus)
    a, b = await reg.get_provider("asp-a"), await reg.get_provider("asp-b")
    await reg.release_provider(a)
    await reg.release_provider(b)

    await reg.invalidate("asp-a")

    assert a.closed and not b.closed
    assert await reg.get_provider("asp-b") is b


@pytest.mark.asyncio
async def test_invalidating_a_provider_that_was_never_cached_is_a_no_op():
    reg = _registry()
    await reg.bind_invalidation_bus(InMemoryInvalidationBus())
    await reg.invalidate("asp-never-built")
    await _registry().invalidate("asp-never-built")


@pytest.mark.asyncio
async def test_a_store_that_fails_to_close_does_not_fail_the_edit():
    """The row is already written when the eviction runs, so a store whose ``aclose`` raises is logged, never turned into a failed PUT."""
    reg = _registry()
    inst = await reg.get_provider("asp-a")

    async def broken():
        raise OSError("close failed")

    inst.aclose = broken
    await reg.invalidate("asp-a")

    assert await reg.get_provider("asp-a") is not inst


@pytest.mark.asyncio
async def test_a_reconnect_flush_drops_every_instance_without_closing_it():
    """A notification dropped during a LISTEN blip may have left a stale instance cached, so a reconnect forgets them all; one may still be in use by an
    in-flight request, so none is closed."""
    reg = _registry({"asp-a": object(), "asp-b": object()})
    a, b = await reg.get_provider("asp-a"), await reg.get_provider("asp-b")

    reg._flush_local()

    assert not a.closed and not b.closed
    assert await reg.get_provider("asp-a") is not a
    assert await reg.get_provider("asp-b") is not b


@pytest.mark.asyncio
async def test_the_bus_is_told_to_flush_on_reconnect():
    bus = _CountingBus()
    reg = _registry()
    await reg.bind_invalidation_bus(bus)

    ((topic, on_reconnect),) = bus.subscribed

    assert topic is InvalidationTopic.ARTIFACT_STORAGE_PROVIDER
    assert on_reconnect == reg._flush_local


class _GatedStub(_StubArtifactStorage):
    """A store whose ``initialize`` waits on ``gate``, so a test can interleave an eviction with a build in flight."""

    def __init__(self, gate):
        super().__init__()
        self._gate = gate

    async def initialize(self):
        await self._gate.wait()
        await super().initialize()


async def _start_a_build_in_flight(reg_rows, gate):
    """A registry whose first build blocks on ``gate``; returns the registry and the task that is building (already inside ``initialize``)."""
    built = []

    def _factory(row, storage_provider):
        inst = _GatedStub(gate)
        built.append(inst)
        return inst

    reg = ArtifactStorageRegistry(storage=_FakeStore(reg_rows), storage_provider=None, factory=_factory)
    task = asyncio.create_task(reg.get_provider("asp-a"))
    async with asyncio.timeout(5):
        while not built:
            await asyncio.sleep(0)
    return reg, task


@pytest.mark.asyncio
async def test_a_get_that_races_a_flush_does_not_cache_its_instance():
    """The racing get built its instance from a row read BEFORE the flush; caching it would keep exactly the staleness the flush clears. The caller still
    gets its instance (its request completes), and the next get builds a fresh one."""
    gate = asyncio.Event()
    reg, task = await _start_a_build_in_flight({"asp-a": object()}, gate)

    reg._flush_local()
    gate.set()
    raced = await task

    assert raced is not await reg.get_provider("asp-a")


@pytest.mark.asyncio
async def test_a_get_that_races_an_invalidation_does_not_cache_its_instance():
    """The same race with an eviction of the provider's own id, which is how another pod's edit arrives: the build in flight started before the edit."""
    gate = asyncio.Event()
    reg, task = await _start_a_build_in_flight({"asp-a": object()}, gate)

    await reg.invalidate("asp-a")
    gate.set()
    raced = await task

    assert raced is not await reg.get_provider("asp-a")


@pytest.mark.asyncio
async def test_a_get_with_no_eviction_in_flight_is_cached():
    gate = asyncio.Event()
    reg, task = await _start_a_build_in_flight({"asp-a": object()}, gate)

    gate.set()

    assert await task is await reg.get_provider("asp-a")


# ---- a store built during an eviction race is closed after its use (slice D ruling 6; the #784 review note) ---------------------------------------
#
# The racing get hands its caller the instance it built (the request completes) but does not cache it. Nothing else holds it, so before slice D
# nothing ever closed it: harmless for the database and filesystem stores, a leaked aiobotocore client (and its connection pool) for S3.


async def _raced(reg_rows=None):
    gate = asyncio.Event()
    reg, task = await _start_a_build_in_flight({"asp-a": object()} if reg_rows is None else reg_rows, gate)
    await reg.invalidate("asp-a")
    gate.set()
    return reg, await task


@pytest.mark.asyncio
async def test_a_store_built_during_an_eviction_race_is_closed_once_its_caller_releases_it():
    reg, raced = await _raced()

    assert not raced.closed, "its caller is still using it"
    await reg.release_provider(raced)

    assert raced.closed
    await reg.release_provider(raced)                      # a second release is harmless


@pytest.mark.asyncio
async def test_releasing_a_cached_store_leaves_it_open_and_cached():
    reg = _registry()
    cached = await reg.get_provider("asp-a")

    await reg.release_provider(cached)

    assert not cached.closed
    assert await reg.get_provider("asp-a") is cached


@pytest.mark.asyncio
async def test_a_raced_store_that_is_never_released_is_closed_with_the_registry():
    reg, raced = await _raced()

    await reg.aclose()

    assert raced.closed


@pytest.mark.asyncio
async def test_a_raced_store_whose_close_fails_is_logged_not_raised(caplog):
    reg, raced = await _raced()

    async def broken():
        raise OSError("close failed")

    raced.aclose = broken
    await reg.release_provider(raced)

    assert "close failed" in caplog.text


class _GatedMem(MemStore):
    """A routing-store backend (it needs a location record) whose ``initialize`` waits on ``gate``."""

    def __init__(self, gate):
        super().__init__()
        self._gate = gate

    async def initialize(self):
        await self._gate.wait()


@pytest.mark.asyncio
async def test_the_routing_store_closes_a_raced_store_after_the_put_that_used_it(tmp_path):
    """End to end through ``get_default()``: a put resolves the active provider while an edit of that provider evicts it. The put completes on
    the store it built, which is closed once the put is done; the next put builds a fresh store, which is cached and stays open."""
    from primer.model.artifact import ArtifactLocation
    from primer.model.provider import ArtifactStorageProvider, SqliteConfig
    from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "race.sqlite"))
    await sp.initialize()
    try:
        providers = sp.get_storage(ArtifactStorageProvider)
        await providers.create(ArtifactStorageProvider(id="mem-1", provider="filesystem", config={"root": str(tmp_path / "unused")}))
        await sp.get_storage(ActiveArtifactStorageConfig).create(
            ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="mem-1"),
        )
        gate, built = asyncio.Event(), []

        def factory(row, storage_provider):
            built.append(_GatedMem(gate))
            return built[-1]

        reg = ArtifactStorageRegistry(storage=providers, storage_provider=sp, factory=factory)
        routing = await reg.get_default()
        put = asyncio.create_task(routing.put(data=b"raced", mime_type="text/plain"))
        async with asyncio.timeout(5):
            while not built:
                await asyncio.sleep(0)
        await reg.invalidate("mem-1")
        gate.set()
        aid = await put

        assert built[0].closed, "the raced store was closed after the put"
        assert (await sp.get_storage(ArtifactLocation).get(aid)).provider_id == "mem-1"

        second = await routing.put(data=b"cached", mime_type="text/plain")
        assert len(built) == 2 and not built[1].closed, "a store built with no race is cached and stays open"
        assert (await routing.get(second)).data == b"cached"
        assert not built[1].closed
        await reg.aclose()
        assert built[1].closed
    finally:
        await sp.aclose()


# ---- leases: a store is never closed under a call in flight (slice D round 2, the lead's ruling on #899) -----------------------------------------
#
# The review of #899 found an edit closing a cached S3 store under an in-flight put (whose object then could not be removed) or get, and a
# reconnect flush dropping every cached store without ever closing it. Every ``get_provider`` now takes a lease and ``release_provider``
# returns it; an eviction or a flush retires a leased store, and it is closed when its last lease returns.


@pytest.mark.asyncio
async def test_an_eviction_retires_a_leased_store_and_its_last_release_closes_it():
    reg = _registry()
    first, second = await reg.get_provider("asp-a"), await reg.get_provider("asp-a")
    assert first is second

    await reg.invalidate("asp-a")
    assert not first.closed, "two calls hold it"
    fresh = await reg.get_provider("asp-a")
    assert fresh is not first, "the next get builds a fresh store from the current row"

    await reg.release_provider(first)
    assert not first.closed, "one call still holds it"
    await reg.release_provider(second)
    assert first.closed
    assert not fresh.closed


@pytest.mark.asyncio
async def test_a_flush_closes_an_idle_store_and_retires_a_leased_one():
    reg = _registry({"asp-a": object(), "asp-b": object()})
    idle = await reg.get_provider("asp-a")
    await reg.release_provider(idle)
    busy = await reg.get_provider("asp-b")

    reg._flush_local()
    async with asyncio.timeout(5):
        while not idle.closed:
            await asyncio.sleep(0)

    assert not busy.closed
    await reg.release_provider(busy)
    assert busy.closed


@pytest.mark.asyncio
async def test_aclose_closes_cached_retired_and_flushed_stores():
    reg = _registry({"asp-a": object(), "asp-b": object(), "asp-c": object()})
    leased_and_flushed = await reg.get_provider("asp-a")
    evicted_while_leased = await reg.get_provider("asp-b")
    await reg.invalidate("asp-b")
    reg._flush_local()
    cached = await reg.get_provider("asp-c")

    await reg.aclose()

    assert leased_and_flushed.closed and evicted_while_leased.closed and cached.closed


@pytest.mark.asyncio
async def test_a_second_release_of_a_retired_store_closes_it_once():
    reg = _registry()
    closes = []
    store = await reg.get_provider("asp-a")
    real = store.aclose

    async def counting():
        closes.append(1)
        await real()

    store.aclose = counting
    await reg.invalidate("asp-a")

    await reg.release_provider(store)
    await reg.release_provider(store)
    await reg.aclose()

    assert closes == [1]


@pytest.mark.asyncio
async def test_a_reconnect_flush_drops_every_instance_and_never_closes_a_leased_one():
    """The flush test above, with both stores leased: neither is closed, and the next get builds fresh ones."""
    reg = _registry({"asp-a": object(), "asp-b": object()})
    a, b = await reg.get_provider("asp-a"), await reg.get_provider("asp-b")

    reg._flush_local()

    assert not a.closed and not b.closed
    assert await reg.get_provider("asp-a") is not a


@pytest.mark.asyncio
async def test_binding_twice_subscribes_once():
    bus = _CountingBus()
    instances = []
    reg = _registry(instances=instances)
    await reg.bind_invalidation_bus(bus)
    await reg.bind_invalidation_bus(bus)
    await reg.release_provider(await reg.get_provider("asp-a"))

    await reg.invalidate("asp-a")

    assert len(bus.subscribed) == 1
    assert instances[0].closed


@pytest.mark.asyncio
async def test_aclose_unsubscribes_and_closes_every_cached_instance():
    bus = _CountingBus()
    reg = _registry()
    await reg.bind_invalidation_bus(bus)
    inst = await reg.get_provider("asp-a")

    await reg.aclose()

    assert inst.closed
    fresh = await reg.get_provider("asp-a")
    await bus.publish(InvalidationTopic.ARTIFACT_STORAGE_PROVIDER, "asp-a")
    assert not fresh.closed, "the closed registry no longer listens to the bus"


@pytest.mark.asyncio
async def test_a_closed_registry_invalidates_locally_again():
    """After ``aclose`` the subscription is gone, so an ``invalidate`` that only published would evict nothing."""
    reg = _registry()
    await reg.bind_invalidation_bus(InMemoryInvalidationBus())
    await reg.aclose()
    inst = await reg.get_provider("asp-a")
    await reg.release_provider(inst)

    await reg.invalidate("asp-a")

    assert inst.closed
