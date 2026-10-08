"""DbArtifactStorage put/get/delete round-trip + autogen id."""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.artifact.db import DbArtifactStorage
from primer.artifact.factory import build_artifact_storage
from primer.model.except_ import ConfigError
from primer.model.provider import (
    ArtifactStorageProvider, FilesystemArtifactConfig, S3ArtifactConfig,
    SqliteConfig,
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
async def test_factory_db_backend(sp: SqliteStorageProvider):
    row = ArtifactStorageProvider(id="asp-1", provider="db")
    store = build_artifact_storage(row, storage_provider=sp)
    assert isinstance(store, DbArtifactStorage)


def test_factory_unimplemented_backends_raise():
    fs = ArtifactStorageProvider(
        id="asp-fs", provider="filesystem",
        config=FilesystemArtifactConfig(root="/tmp/x"))
    s3 = ArtifactStorageProvider(
        id="asp-s3", provider="s3", config=S3ArtifactConfig(bucket="b"))
    with pytest.raises(ConfigError):
        build_artifact_storage(fs, storage_provider=None)
    with pytest.raises(ConfigError):
        build_artifact_storage(s3, storage_provider=None)
