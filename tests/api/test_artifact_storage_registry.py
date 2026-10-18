"""ArtifactStorageRegistry: cache, invalidate, default resolution."""

from __future__ import annotations

from uuid import uuid4

import pytest

from primer.api.registries.artifact_storage_registry import (
    DEFAULT_ARTIFACT_PROVIDER_ID, ArtifactStorageRegistry,
)
from primer.artifact.routing import RoutingArtifactStorage
from primer.model.except_ import NotFoundError
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
    rows = {"asp-a": object()}
    instances = []
    reg = ArtifactStorageRegistry(
        storage=_FakeStore(rows), storage_provider=None,
        factory=_factory_returning(instances))
    await reg.get_provider("asp-a")
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
