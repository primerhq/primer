"""DbArtifactStorage put/get/delete round-trip + autogen id."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.artifact.db import DbArtifactStorage
from primer.artifact.factory import build_artifact_storage
from primer.model.except_ import ConfigError
from primer.model.provider import (
    ArtifactStorageProvider, S3ArtifactConfig, SqliteConfig,
)
from primer.storage.sqlite import SqliteStorageProvider


@pytest.fixture
async def sp(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "a.sqlite"))
    await p.initialize()
    yield p
    await p.aclose()


@pytest.mark.asyncio
async def test_put_get_delete_round_trip(sp: SqliteStorageProvider):
    store = DbArtifactStorage(sp)
    aid = await store.put(data=b"\x89PNG\r\n", mime_type="image/png", filename="x.png")
    assert aid.startswith("artifact-")
    blob = await store.get(aid)
    assert blob is not None
    assert blob.data == b"\x89PNG\r\n"
    assert blob.mime_type == "image/png"
    assert blob.filename == "x.png"
    await store.delete(aid)
    assert await store.get(aid) is None


@pytest.mark.asyncio
async def test_get_unknown_returns_none(sp: SqliteStorageProvider):
    store = DbArtifactStorage(sp)
    assert await store.get("artifact-nope") is None


@pytest.mark.asyncio
async def test_delete_of_a_missing_id_is_a_no_op(sp: SqliteStorageProvider):
    """The interface says a delete of an unknown id does nothing; ``Storage.delete`` raises ``NotFoundError`` for one, so the backend must absorb it
    (the routing store sends every unrecorded id here, and a second delete of the same artifact must not fail)."""
    store = DbArtifactStorage(sp)
    await store.delete("artifact-nope")
    aid = await store.put(data=b"x", mime_type="text/plain", filename=None)
    await store.delete(aid)
    await store.delete(aid)
    assert await store.get(aid) is None


@pytest.mark.asyncio
async def test_factory_db_backend(sp: SqliteStorageProvider):
    row = ArtifactStorageProvider(id="asp-1", provider="db")
    store = build_artifact_storage(row, storage_provider=sp)
    assert isinstance(store, DbArtifactStorage)


def test_factory_s3_backend_is_not_built_yet():
    """The filesystem backend builds since plan 09 Task 8 (tests/artifact/test_checks.py pins its class); only s3 still raises."""
    s3 = ArtifactStorageProvider(
        id="asp-s3", provider="s3", config=S3ArtifactConfig(bucket="b"))
    with pytest.raises(ConfigError, match="'s3' is not implemented"):
        build_artifact_storage(s3, storage_provider=None)
