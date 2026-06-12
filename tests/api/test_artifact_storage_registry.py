"""ArtifactStorageRegistry: cache, invalidate, default resolution."""

from __future__ import annotations

import pytest

from primer.api.registries.artifact_storage_registry import (
    DEFAULT_ARTIFACT_PROVIDER_ID, ArtifactStorageRegistry,
)
from primer.model.except_ import NotFoundError


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
async def test_get_default_resolves_reserved_id():
    rows = {DEFAULT_ARTIFACT_PROVIDER_ID: object()}
    instances = []
    reg = ArtifactStorageRegistry(
        storage=_FakeStore(rows), storage_provider=None,
        factory=_factory_returning(instances))
    await reg.get_default()
    assert len(instances) == 1


@pytest.mark.asyncio
async def test_unknown_id_raises():
    reg = ArtifactStorageRegistry(
        storage=_FakeStore({}), storage_provider=None,
        factory=_factory_returning([]))
    with pytest.raises(NotFoundError):
        await reg.get_provider("asp-missing")
