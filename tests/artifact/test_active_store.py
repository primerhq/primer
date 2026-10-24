"""The active artifact-storage setting selects where new artifacts are written."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.artifact.active import read_active_artifact_store, set_active_artifact_store
from primer.artifact.db import DbArtifactStorage
from primer.artifact.probe import ArtifactProbeResult
from primer.artifact.routing import RoutingArtifactStorage
from primer.common.entity_checks import EntityCheckError
from primer.model.artifact import Artifact, ArtifactLocation
from primer.model.provider import ArtifactStorageProvider, SqliteConfig
from primer.model.providers.artifact import (
    ACTIVE_ARTIFACT_STORAGE_CONFIG_ID,
    DEFAULT_ARTIFACT_PROVIDER_ID,
    ActiveArtifactStorageConfig,
)
from primer.storage.sqlite import SqliteStorageProvider
from tests.artifact._stores import MemStore


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "active.sqlite"))
    await sp.initialize()
    mem = MemStore()
    await sp.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider(id="mem-1", provider="filesystem", config={"root": str(tmp_path)}),
    )

    async def resolve(provider_id: str):
        assert provider_id == "mem-1"
        return mem

    routing = RoutingArtifactStorage(
        resolve=resolve,
        fallback=DbArtifactStorage(sp),
        locations=sp.get_storage(ArtifactLocation),
        active_config=sp.get_storage(ActiveArtifactStorageConfig),
        providers=sp.get_storage(ArtifactStorageProvider),
    )
    yield sp, mem, routing
    await sp.aclose()


async def _passes(*args, **kwargs) -> ArtifactProbeResult:
    return ArtifactProbeResult(ok=True)


async def test_read_synthesises_the_default_row_when_none_is_stored(world) -> None:
    sp, _, _ = world
    row = await read_active_artifact_store(sp)
    assert row.id == ACTIVE_ARTIFACT_STORAGE_CONFIG_ID
    assert row.provider_id == DEFAULT_ARTIFACT_PROVIDER_ID


async def test_after_a_set_new_puts_land_in_the_selected_store(world) -> None:
    sp, mem, routing = world
    await set_active_artifact_store(sp, "mem-1", probe=_passes)

    artifact_id = await routing.put(data=b"active", mime_type="text/plain")

    assert mem.blobs[artifact_id].data == b"active"
    assert await sp.get_storage(Artifact).get(artifact_id) is None


async def test_set_refuses_an_unknown_provider(world) -> None:
    sp, _, _ = world
    with pytest.raises(EntityCheckError) as caught:
        await set_active_artifact_store(sp, "missing", probe=_passes)
    assert caught.value.code == "unknown_provider_id"
    assert caught.value.field == "provider_id"
