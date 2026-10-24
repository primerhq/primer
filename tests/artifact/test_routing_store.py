"""The routing store behind ``ArtifactStorageRegistry.get_default()`` (plan 09, Task 2; design section 15.2).

Every consumer keeps calling ``get_default()`` and gets one object that routes by a per-artifact record:

* ``put`` writes to the ACTIVE provider (a singleton row, read on every put and never cached) and, for a backend the database cannot serve
  (``needs_location_record``), records which provider holds the new id;
* ``get`` and ``delete`` follow the record, so an artifact stays readable after the active store changes;
* an id with no record is a database artifact, served by a DIRECT ``DbArtifactStorage`` and never by a registry lookup.

The world is a real SQLite storage (the records are real rows), an in-memory backend registered as ``mem-1`` (like the filesystem and S3 stores it
needs a record) and a database-kind provider registered as ``db-2`` (it needs none).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from primer.artifact.checks import check_artifact_provider_delete
from primer.artifact.db import DbArtifactStorage
from primer.artifact.routing import RoutingArtifactStorage, count_artifacts_held
from primer.int.artifact_storage import ArtifactStorage
from primer.model.artifact import Artifact, ArtifactLocation
from primer.model.except_ import NotFoundError
from primer.model.provider import SqliteConfig
from primer.model.providers.artifact import (
    ACTIVE_ARTIFACT_STORAGE_CONFIG_ID,
    DEFAULT_ARTIFACT_PROVIDER_ID,
    ActiveArtifactStorageConfig,
)
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider
from tests.artifact._stores import MemStore


@dataclass
class World:
    sp: SqliteStorageProvider
    mem: MemStore
    routing: RoutingArtifactStorage
    asked: list[str] = field(default_factory=list)   # the provider ids resolve() was asked for, in order

    @property
    def locations(self):
        return self.sp.get_storage(ArtifactLocation)

    @property
    def artifacts(self):
        return self.sp.get_storage(Artifact)


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "a.sqlite"))
    await sp.initialize()
    mem = MemStore()
    stores: dict[str, ArtifactStorage] = {"mem-1": mem, "db-2": DbArtifactStorage(sp)}
    asked: list[str] = []

    async def resolve(provider_id: str) -> ArtifactStorage:
        asked.append(provider_id)
        try:
            return stores[provider_id]
        except KeyError:
            raise NotFoundError(f"ArtifactStorageProvider {provider_id!r} does not exist") from None

    routing = RoutingArtifactStorage(
        resolve=resolve,
        fallback=DbArtifactStorage(sp),
        locations=sp.get_storage(ArtifactLocation),
        active_config=sp.get_storage(ActiveArtifactStorageConfig),
    )
    yield World(sp=sp, mem=mem, routing=routing, asked=asked)
    await sp.aclose()


async def _activate(sp: SqliteStorageProvider, provider_id: str) -> None:
    """Write the singleton row the way an operator's selection does: create it, or replace it."""
    active = sp.get_storage(ActiveArtifactStorageConfig)
    row = ActiveArtifactStorageConfig(id=ACTIVE_ARTIFACT_STORAGE_CONFIG_ID, provider_id=provider_id)
    if await active.get(ACTIVE_ARTIFACT_STORAGE_CONFIG_ID) is None:
        await active.create(row)
    else:
        await active.update(row)


async def _put(world: World, data: bytes = b"x", *, mime_type: str = "text/plain", filename: str | None = None) -> str:
    return await world.routing.put(data=data, mime_type=mime_type, filename=filename)


# ---- put ------------------------------------------------------------------------------------------------------------------------------------------


async def test_with_no_active_setting_a_put_goes_to_the_database_and_records_nothing(world: World) -> None:
    aid = await _put(world)

    assert await world.artifacts.get(aid) is not None
    assert await world.locations.get(aid) is None
    assert world.mem.blobs == {}


async def test_a_put_goes_to_the_active_provider_and_records_it(world: World) -> None:
    await _activate(world.sp, "mem-1")

    aid = await _put(world, filename="a.txt")

    assert world.mem.blobs[aid].data == b"x"
    assert world.mem.blobs[aid].filename == "a.txt"
    assert (await world.locations.get(aid)).provider_id == "mem-1"
    assert await world.artifacts.get(aid) is None, "nothing reached the database"


async def test_a_database_kind_active_provider_records_nothing(world: World) -> None:
    """Every ``db`` provider stores in the same ``Artifact`` table, so the fallback reads them all and a spare ``db`` row can always be deleted."""
    await _activate(world.sp, "db-2")

    aid = await _put(world)

    assert await world.artifacts.get(aid) is not None
    assert await world.locations.get(aid) is None
    assert world.asked == ["db-2"], "the provider was resolved by its id, not short-circuited"


async def test_the_reserved_default_named_as_active_writes_straight_to_the_database(world: World) -> None:
    """The fallback is a direct ``DbArtifactStorage`` (plan 09 constraint): the reserved id is never looked up in the registry."""
    await _activate(world.sp, DEFAULT_ARTIFACT_PROVIDER_ID)

    aid = await _put(world)

    assert await world.artifacts.get(aid) is not None
    assert await world.locations.get(aid) is None
    assert world.asked == []


async def test_the_active_setting_is_read_on_every_put_and_never_cached(world: World) -> None:
    """A switch is seen by every process at once because no process remembers the setting."""
    first = await _put(world)
    await _activate(world.sp, "mem-1")
    second = await _put(world)
    await _activate(world.sp, DEFAULT_ARTIFACT_PROVIDER_ID)
    third = await _put(world)

    assert [first in world.mem.blobs, second in world.mem.blobs, third in world.mem.blobs] == [False, True, False]
    assert await world.artifacts.get(first) is not None and await world.artifacts.get(third) is not None
    assert await world.routing.active_provider_id() == DEFAULT_ARTIFACT_PROVIDER_ID


async def test_active_provider_id_reads_the_row_else_the_default(world: World) -> None:
    assert await world.routing.active_provider_id() == DEFAULT_ARTIFACT_PROVIDER_ID
    await _activate(world.sp, "mem-1")
    assert await world.routing.active_provider_id() == "mem-1"


async def test_a_put_to_an_active_provider_that_cannot_be_resolved_fails_and_writes_nothing(world: World) -> None:
    """The active store is the operator's choice. If it cannot be built, the write fails loudly instead of landing somewhere the operator did not pick."""
    await _activate(world.sp, "gone-1")

    with pytest.raises(NotFoundError):
        await _put(world)

    assert (await world.artifacts.list(OffsetPage(offset=0, length=1))).total == 0
    assert (await world.locations.list(OffsetPage(offset=0, length=1))).total == 0
    assert world.mem.blobs == {}


async def test_a_failed_record_write_removes_the_blob_and_raises(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await _activate(world.sp, "mem-1")

    async def down(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(world.locations, "create", down)

    with pytest.raises(RuntimeError, match="db down"):
        await _put(world)

    assert world.mem.blobs == {}, "no file without a record is left behind"


async def test_a_failed_cleanup_after_a_failed_record_write_does_not_hide_the_error(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    await _activate(world.sp, "mem-1")

    async def down(*args, **kwargs):
        raise RuntimeError("db down")

    async def cannot_delete(artifact_id: str) -> None:
        raise OSError("store down too")

    monkeypatch.setattr(world.locations, "create", down)
    monkeypatch.setattr(world.mem, "delete", cannot_delete)

    with caplog.at_level(logging.WARNING, logger="primer.artifact.routing"), pytest.raises(RuntimeError, match="db down"):
        await _put(world)

    assert "store down too" in caplog.text, "the cleanup failure is logged"


async def test_a_cancelled_record_write_removes_the_blob_too(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request cancelled between the file and its record (a client that went away) must not leave an orphan either."""
    await _activate(world.sp, "mem-1")

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(world.locations, "create", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await _put(world)

    assert world.mem.blobs == {}


@pytest.mark.parametrize("failure", [asyncio.CancelledError, ConnectionResetError])
async def test_a_record_that_committed_before_the_put_failed_is_removed_too(
    world: World, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException],
) -> None:
    """The record INSERT commits and the call still fails (a cancel from a client that went away, a connection lost after the commit).
    The bytes were removed but the record stayed, so the provider counted an artifact it did not hold and refused every delete and move
    with "holds 1 artifact(s)", which no one could clear: no id was handed out and no route lists the records (the G5 audit, G5-05)."""
    await _activate(world.sp, "mem-1")
    real_create = world.locations.create

    async def commit_then_fail(row):
        await real_create(row)
        raise failure

    monkeypatch.setattr(world.locations, "create", commit_then_fail)
    with pytest.raises(failure):
        await _put(world)
    monkeypatch.undo()

    assert world.mem.blobs == {}
    assert (await world.locations.list(OffsetPage(offset=0, length=10))).items == [], "the committed record was removed"
    assert await count_artifacts_held(world.sp, "mem-1") == 0
    await _activate(world.sp, DEFAULT_ARTIFACT_PROVIDER_ID)
    await check_artifact_provider_delete(world.sp, "mem-1")          # no "holds 1 artifact(s)" left to block the delete


async def test_a_failed_record_removal_is_logged_and_the_bytes_are_still_removed(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    await _activate(world.sp, "mem-1")
    real_create = world.locations.create

    async def commit_then_fail(row):
        await real_create(row)
        raise RuntimeError("connection lost after the commit")

    async def cannot_delete(artifact_id: str) -> None:
        raise OSError("records table down")

    monkeypatch.setattr(world.locations, "create", commit_then_fail)
    monkeypatch.setattr(world.locations, "delete", cannot_delete)
    with caplog.at_level(logging.WARNING, logger="primer.artifact.routing"), pytest.raises(RuntimeError, match="after the commit"):
        await _put(world)

    assert "records table down" in caplog.text, "the record cleanup failure is logged"
    assert world.mem.blobs == {}, "the bytes are removed whatever the record cleanup did"


# ---- get ------------------------------------------------------------------------------------------------------------------------------------------


async def test_an_id_with_no_record_is_read_from_the_database(world: World) -> None:
    aid = await DbArtifactStorage(world.sp).put(data=b"old", mime_type="image/png", filename=None)
    await _activate(world.sp, "mem-1")

    blob = await world.routing.get(aid)

    assert blob is not None and blob.data == b"old" and blob.mime_type == "image/png"
    assert world.asked == [], "an unrecorded id never goes through the registry"


async def test_reads_follow_the_record_after_the_active_store_changes(world: World) -> None:
    await _activate(world.sp, "mem-1")
    aid = await _put(world, b"new")
    await _activate(world.sp, DEFAULT_ARTIFACT_PROVIDER_ID)

    blob = await world.routing.get(aid)

    assert blob is not None and blob.data == b"new"


async def test_an_unknown_id_reads_as_missing(world: World) -> None:
    assert await world.routing.get("artifact-" + "0" * 32) is None
    assert await world.routing.get("artifact-nope") is None


async def test_a_record_whose_provider_is_gone_reads_as_missing(world: World, caplog: pytest.LogCaptureFixture) -> None:
    aid = "artifact-" + "b" * 32
    await world.locations.create(ArtifactLocation(id=aid, provider_id="gone-1"))

    with caplog.at_level(logging.WARNING, logger="primer.artifact.routing"):
        assert await world.routing.get(aid) is None
        await world.routing.delete(aid)

    assert "gone-1" in caplog.text and aid in caplog.text
    assert await world.locations.get(aid) is not None, "the record stays: the bytes may be reachable again when the provider is back"


# ---- delete ---------------------------------------------------------------------------------------------------------------------------------------


async def test_a_delete_follows_the_record_and_drops_it(world: World) -> None:
    await _activate(world.sp, "mem-1")
    aid = await _put(world)

    await world.routing.delete(aid)

    assert world.mem.blobs == {}
    assert await world.locations.get(aid) is None
    assert await world.routing.get(aid) is None


async def test_a_delete_of_an_unrecorded_id_deletes_the_database_artifact(world: World) -> None:
    aid = await DbArtifactStorage(world.sp).put(data=b"old", mime_type="text/plain", filename=None)

    await world.routing.delete(aid)

    assert await world.artifacts.get(aid) is None
    assert world.asked == []


async def test_deleting_an_unknown_id_is_a_no_op(world: World) -> None:
    await world.routing.delete("artifact-" + "0" * 32)
    await world.routing.delete("artifact-nope")


async def test_deleting_twice_is_a_no_op(world: World) -> None:
    await _activate(world.sp, "mem-1")
    aid = await _put(world)

    await world.routing.delete(aid)
    await world.routing.delete(aid)

    assert world.mem.blobs == {}


async def test_a_failed_store_delete_keeps_the_record_so_a_retry_can_finish(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await _activate(world.sp, "mem-1")
    aid = await _put(world)
    real_delete = world.mem.delete

    async def down(artifact_id: str) -> None:
        raise OSError("store down")

    monkeypatch.setattr(world.mem, "delete", down)
    with pytest.raises(OSError, match="store down"):
        await world.routing.delete(aid)
    assert await world.locations.get(aid) is not None and aid in world.mem.blobs, "the bytes are still reachable"

    monkeypatch.setattr(world.mem, "delete", real_delete)
    await world.routing.delete(aid)
    assert world.mem.blobs == {} and await world.locations.get(aid) is None


# ---- lifecycle ------------------------------------------------------------------------------------------------------------------------------------


async def test_the_routing_store_holds_no_resources_and_closes_nothing_it_routes_to(world: World) -> None:
    await _activate(world.sp, "mem-1")
    await _put(world)

    await world.routing.initialize()
    await world.routing.aclose()
    await world.routing.aclose()

    assert world.mem.closed is False, "the registry owns the provider instances, not the router"


def test_the_flags_say_which_backends_need_a_record() -> None:
    assert ArtifactStorage.needs_location_record is True
    assert MemStore.needs_location_record is True
    assert DbArtifactStorage.needs_location_record is False
    assert RoutingArtifactStorage.needs_location_record is False, "the router writes the records itself; it is never wrapped in another"


# ---- count_artifacts_held -------------------------------------------------------------------------------------------------------------------------


async def test_count_artifacts_held_counts_only_that_provider(world: World) -> None:
    for artifact_id, provider_id in (("artifact-" + "1" * 32, "mem-1"), ("artifact-" + "2" * 32, "mem-1"), ("artifact-" + "3" * 32, "x")):
        await world.locations.create(ArtifactLocation(id=artifact_id, provider_id=provider_id))

    assert await count_artifacts_held(world.sp, "mem-1") == 2
    assert await count_artifacts_held(world.sp, "x") == 1
    assert await count_artifacts_held(world.sp, "none") == 0


async def test_count_artifacts_held_follows_puts_and_deletes(world: World) -> None:
    await _activate(world.sp, "mem-1")
    first, _ = await _put(world), await _put(world)
    assert await count_artifacts_held(world.sp, "mem-1") == 2

    await world.routing.delete(first)

    assert await count_artifacts_held(world.sp, "mem-1") == 1
