"""The contract every artifact backend keeps, run against each backend that builds (plan 09, Task 8; Task 9 adds ``s3`` to the params).

A consumer never knows which store holds its file, so each backend must hand back exactly what it was given: the bytes, the MIME type and the
filename, whatever characters the filename holds (Review Focus 4: accents, quotes, a newline and CJK), and ``None`` for a missing one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from primer.artifact.db import DbArtifactStorage
from primer.artifact.filesystem import FilesystemArtifactStorage, prepare_root
from primer.int.artifact_storage import ArtifactBlob, ArtifactStorage
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


@pytest.fixture(params=["db", "filesystem"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[ArtifactStorage]:
    if request.param == "db":
        sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "contract.sqlite"))
        await sp.initialize()
        backend: ArtifactStorage = DbArtifactStorage(sp)
        await backend.initialize()
        yield backend
        await backend.aclose()
        await sp.aclose()
    else:
        prepare_root(str(tmp_path / "root"))
        backend = FilesystemArtifactStorage(str(tmp_path / "root"))
        await backend.initialize()
        yield backend
        await backend.aclose()


async def test_a_round_trip_keeps_bytes_mime_and_filename(store: ArtifactStorage) -> None:
    data = bytes(range(256)) * 3

    aid = await store.put(data=data, mime_type="application/pdf", filename="report.pdf")

    assert await store.get(aid) == ArtifactBlob(data=data, mime_type="application/pdf", filename="report.pdf")


@pytest.mark.parametrize("filename", ["résumé 1.png", 'quote " and ; semicolon.txt', "line\nbreak.txt", "日本語.pdf"])
async def test_any_filename_round_trips(store: ArtifactStorage, filename: str) -> None:
    aid = await store.put(data=b"x", mime_type="image/png", filename=filename)

    assert (await store.get(aid)).filename == filename


async def test_a_missing_filename_reads_back_as_none(store: ArtifactStorage) -> None:
    aid = await store.put(data=b"x", mime_type="text/plain")

    assert (await store.get(aid)).filename is None


async def test_empty_bytes_round_trip(store: ArtifactStorage) -> None:
    aid = await store.put(data=b"", mime_type="application/octet-stream", filename="empty")

    assert await store.get(aid) == ArtifactBlob(data=b"", mime_type="application/octet-stream", filename="empty")


async def test_an_unknown_id_reads_none_and_deletes_quietly(store: ArtifactStorage) -> None:
    unknown = "artifact-" + "0" * 32

    assert await store.get(unknown) is None
    await store.delete(unknown)


async def test_a_deleted_artifact_reads_none(store: ArtifactStorage) -> None:
    aid = await store.put(data=b"gone", mime_type="text/plain")

    await store.delete(aid)
    await store.delete(aid)

    assert await store.get(aid) is None


async def test_two_puts_of_the_same_bytes_get_two_ids(store: ArtifactStorage) -> None:
    first = await store.put(data=b"same", mime_type="text/plain")
    second = await store.put(data=b"same", mime_type="text/plain")

    assert first != second
    await store.delete(first)
    assert (await store.get(second)).data == b"same", "deleting one copy leaves the other"


async def test_initialize_and_aclose_are_idempotent(store: ArtifactStorage) -> None:
    await store.initialize()
    await store.initialize()
    aid = await store.put(data=b"x", mime_type="text/plain")
    await store.aclose()
    await store.aclose()

    assert aid.startswith("artifact-")
