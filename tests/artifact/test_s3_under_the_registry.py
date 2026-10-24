"""The S3 store under the real ``ArtifactStorageRegistry`` and its routing store (``get_default()``), on moto (slice D round 2, the review of #899).

An edit of a provider evicts its cached store, and a reconnect of the invalidation bus flushes them all. Before the lease rule, an eviction closed
the cached S3 store under the calls using it: a put whose row was moved meanwhile could not remove its object (the #890 rule), and an edit that
moved nothing failed an in-flight get or put. A flush dropped every cached store without closing it, so their clients leaked. Every call now
holds a lease on its store for the whole call; an eviction or a flush retires a leased store, and it is closed when its last lease returns.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from primer.api.registries.artifact_storage_registry import ArtifactStorageRegistry
from primer.artifact.s3 import S3ArtifactStorage
from primer.int.artifact_storage import ArtifactBlob
from primer.model.artifact import ArtifactLocation
from primer.model.except_ import ConflictError
from primer.model.provider import ArtifactStorageProvider, SqliteConfig
from primer.model.providers.artifact import ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, ActiveArtifactStorageConfig
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider
from tests.artifact.conftest import S3_BUCKET, S3_TEST_KEY, S3_TEST_SECRET, s3_keys_under


def _row(provider_id: str, endpoint: str, prefix: str, **over) -> ArtifactStorageProvider:
    config = {"bucket": S3_BUCKET, "prefix": prefix, "endpoint_url": endpoint, "access_key": S3_TEST_KEY, "secret_key": S3_TEST_SECRET, **over}
    return ArtifactStorageProvider.model_validate({"id": provider_id, "provider": "s3", "config": config})


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "registry.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


@pytest.fixture
async def world(sp, s3_endpoint: str, s3_prefix: str, isolated_aws_env: None):
    """One active S3 provider ``s3-1`` on the session's bucket, the real registry, and its routing store."""
    providers = sp.get_storage(ArtifactStorageProvider)
    await providers.create(_row("s3-1", s3_endpoint, s3_prefix))
    await sp.get_storage(ActiveArtifactStorageConfig).create(
        ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id="s3-1"),
    )
    registry = ArtifactStorageRegistry(storage=providers, storage_provider=sp)
    routing = await registry.get_default()
    yield providers, registry, routing
    await registry.aclose()


async def _cached_store(registry: ArtifactStorageRegistry) -> S3ArtifactStorage:
    """The cached store of ``s3-1``, with the test's own lease returned at once."""
    store = await registry.get_provider("s3-1")
    await registry.release_provider(store)
    assert isinstance(store, S3ArtifactStorage)
    return store


async def test_a_move_during_a_put_leaves_no_object_and_no_record(world, sp, s3_endpoint: str, s3_prefix: str, s3_admin) -> None:
    """What ``PUT /v1/artifact_storage_providers/s3-1`` does while a put is between its object and its record: write the moved row, then
    evict. The put's store stays open under its lease, so the put finds the row moved, removes its object and fails with a 409."""
    providers, registry, routing = world
    store = await _cached_store(registry)
    entered, release = asyncio.Event(), asyncio.Event()
    real_create = routing._locations.create

    async def held_create(entity):
        entered.set()
        await release.wait()
        return await real_create(entity)

    routing._locations.create = held_create
    put = asyncio.create_task(routing.put(data=b"moved", mime_type="text/plain"))
    async with asyncio.timeout(10):
        await entered.wait()
    assert len(s3_keys_under(s3_admin, s3_prefix)) == 1, "the object is written, its record not yet"

    await providers.update(_row("s3-1", s3_endpoint, s3_prefix + "moved/"))
    await registry.invalidate("s3-1")
    assert not store.closed, "the put holds a lease on it"
    release.set()
    with pytest.raises(ConflictError, match="was moved to another location"):
        await put

    assert s3_keys_under(s3_admin, s3_prefix) == []
    assert (await sp.get_storage(ArtifactLocation).list(OffsetPage(length=10))).total == 0
    assert store.closed, "the retired store is closed once the put returns its lease"


async def test_an_edit_that_moves_nothing_during_a_get_lets_the_read_finish(world) -> None:
    _, registry, routing = world
    data = b"x" * 200_000
    aid = await routing.put(data=data, mime_type="text/plain")
    store = await _cached_store(registry)
    entered, release = asyncio.Event(), asyncio.Event()
    real_get = store._client.get_object

    async def held_get(**kwargs):
        response = await real_get(**kwargs)
        entered.set()
        await release.wait()
        return response

    store._client.get_object = held_get
    got = asyncio.create_task(routing.get(aid))
    async with asyncio.timeout(10):
        await entered.wait()
    await registry.invalidate("s3-1")                  # new keys, say: the location does not move
    release.set()

    assert await got == ArtifactBlob(data=data, mime_type="text/plain", filename=None)
    assert store.closed


async def test_an_edit_that_moves_nothing_during_a_put_lets_the_write_finish(world, s3_prefix: str, s3_admin) -> None:
    _, registry, routing = world
    store = await _cached_store(registry)
    entered, release = asyncio.Event(), asyncio.Event()
    real_put = store._client.put_object

    async def held_put(**kwargs):
        entered.set()
        await release.wait()
        return await real_put(**kwargs)

    store._client.put_object = held_put
    put = asyncio.create_task(routing.put(data=b"w", mime_type="text/plain"))
    async with asyncio.timeout(10):
        await entered.wait()
    await registry.invalidate("s3-1")
    release.set()
    aid = await put

    assert s3_keys_under(s3_admin, s3_prefix) == [s3_prefix + aid]
    assert store.closed
    assert (await routing.get(aid)).data == b"w", "the next call builds a fresh store and reads the object"


async def test_a_flush_closes_every_cached_s3_client(world) -> None:
    """The bus reconnect hook drops every cached store: an idle one is closed, a leased one when its call ends; ``aclose`` reaches both."""
    _, registry, routing = world
    idle = await _cached_store(registry)
    registry._flush_local()
    async with asyncio.timeout(10):
        while not idle.closed:
            await asyncio.sleep(0.01)
    assert idle._client is None

    leased = await registry.get_provider("s3-1")
    assert leased is not idle
    registry._flush_local()
    assert not leased.closed
    await registry.aclose()

    assert leased.closed and leased._client is None
