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


# ---- the G5 audit, G5-06: the probe takes time, and the world can change during it ------------------------------------------------------------


async def test_a_provider_deleted_during_the_probe_is_not_selected(world) -> None:
    """The probe can take seconds (an S3 round trip). A delete that runs during it passes its own check, because the provider is not active
    yet; the selection used to be written anyway, naming a provider that no longer existed, and every upload failed until an admin re-selected.
    The provider row is read again after the probe: a 409 (conflict), and the selection is unchanged."""
    from primer.artifact.checks import check_artifact_provider_delete

    sp, mem, routing = world

    async def probe_while_deleted(row, *, storage_provider):
        await check_artifact_provider_delete(storage_provider, row.id)      # the concurrent delete passes: not active yet
        await storage_provider.get_storage(ArtifactStorageProvider).delete(row.id)
        return ArtifactProbeResult(ok=True)

    with pytest.raises(EntityCheckError) as caught:
        await set_active_artifact_store(sp, "mem-1", probe=probe_while_deleted)

    assert (caught.value.kind, caught.value.code, caught.value.field) == ("conflict", "artifact_store_deleted", "provider_id")
    assert (await read_active_artifact_store(sp)).provider_id == DEFAULT_ARTIFACT_PROVIDER_ID
    artifact_id = await routing.put(data=b"chat image", mime_type="image/png")    # uploads keep working
    assert await sp.get_storage(Artifact).get(artifact_id) is not None and mem.blobs == {}


async def test_a_selection_written_by_someone_else_during_the_probe_wins(world) -> None:
    """The selection is a compare-and-set against what was read before the probe: a selection someone else wrote meanwhile is not
    overwritten from a stale read, and this one answers a 409."""
    sp, _, _ = world
    for provider_id in ("db-3", "db-5"):
        await sp.get_storage(ArtifactStorageProvider).create(ArtifactStorageProvider(id=provider_id, provider="db"))
    await set_active_artifact_store(sp, "db-3", probe=_passes)

    async def probe_while_another_is_selected(row, *, storage_provider):
        await set_active_artifact_store(storage_provider, "db-5", probe=_passes)
        return ArtifactProbeResult(ok=True)

    with pytest.raises(EntityCheckError) as caught:
        await set_active_artifact_store(sp, "mem-1", probe=probe_while_another_is_selected)

    assert (caught.value.kind, caught.value.code) == ("conflict", "active_artifact_store_changed")
    assert (await read_active_artifact_store(sp)).provider_id == "db-5"


async def test_a_first_selection_raced_by_another_first_selection_is_a_conflict(world) -> None:
    """No row yet on both sides: the second create finds the first one's row, and answers a 409 instead of a 500."""
    sp, _, _ = world

    async def probe_while_another_is_selected(row, *, storage_provider):
        await storage_provider.get_storage(ArtifactStorageProvider).create(ArtifactStorageProvider(id="db-4", provider="db"))
        await set_active_artifact_store(storage_provider, "db-4", probe=_passes)
        return ArtifactProbeResult(ok=True)

    with pytest.raises(EntityCheckError) as caught:
        await set_active_artifact_store(sp, "mem-1", probe=probe_while_another_is_selected)

    assert caught.value.code == "active_artifact_store_changed"
    assert (await read_active_artifact_store(sp)).provider_id == "db-4"


async def test_selecting_the_store_already_active_again_succeeds(world) -> None:
    sp, _, _ = world
    await sp.get_storage(ArtifactStorageProvider).create(ArtifactStorageProvider(id="db-3", provider="db"))
    await set_active_artifact_store(sp, "mem-1", probe=_passes)
    assert (await set_active_artifact_store(sp, "mem-1", probe=_passes)).provider_id == "mem-1"
    assert (await set_active_artifact_store(sp, "db-3", probe=_passes)).provider_id == "db-3"
    assert (await read_active_artifact_store(sp)).provider_id == "db-3"
