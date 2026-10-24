"""The shared live probe for artifact-storage provider rows."""

from pathlib import Path

import pytest

from primer.artifact.probe import (
    ArtifactProbeResult,
    probe_artifact_provider,
    probe_failure_text,
)
from primer.model.artifact import Artifact
from primer.model.except_ import ConfigError
from primer.model.provider import ArtifactStorageProvider, S3ArtifactConfig, SqliteConfig
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider
from tests.artifact._stores import MemStore


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "probe.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


def _row() -> ArtifactStorageProvider:
    return ArtifactStorageProvider(id="mem-probe", provider="db", config={})


async def test_a_probe_of_the_database_store_passes_and_leaves_nothing(sp) -> None:
    result = await probe_artifact_provider(_row(), storage_provider=sp)

    assert result == ArtifactProbeResult(ok=True)
    assert (await sp.get_storage(Artifact).list(OffsetPage(length=10))).total == 0


async def test_a_probe_that_reads_back_wrong_bytes_names_get_and_cleans_up(sp) -> None:
    store = MemStore(corrupt_reads=True)

    result = await probe_artifact_provider(
        _row(), storage_provider=sp, build=lambda row, storage_provider: store,
    )

    assert (result.ok, result.step) == (False, "get")
    assert store.blobs == {}
    assert store.closed


async def test_a_backend_that_cannot_be_built_names_build(sp) -> None:
    def broken(row, storage_provider):
        raise ConfigError("nope")

    result = await probe_artifact_provider(_row(), storage_provider=sp, build=broken)

    assert (result.ok, result.step, result.error) == (
        False,
        "build",
        "ConfigError: nope",
    )


def test_the_failure_text_masks_the_s3_keys() -> None:
    config = S3ArtifactConfig(
        bucket="b",
        access_key="AKIAEXAMPLEKEY1",
        secret_key="secret-example-1",
    )

    text = probe_failure_text(
        RuntimeError("bad AKIAEXAMPLEKEY1 secret-example-1"), config,
    )

    assert text.startswith("RuntimeError: ")
    assert "AKIAEXAMPLEKEY1" not in text
    assert "secret-example-1" not in text
    assert "[REDACTED]" in text


# ---- the draft Test prepares a filesystem root first (plan 09 Task 8) -------------------------------------------------------------------------


def _fs_row(root: Path) -> ArtifactStorageProvider:
    return ArtifactStorageProvider(id="fs-probe", provider="filesystem", config={"root": str(root)})


async def test_a_draft_probe_prepares_the_root_and_a_saved_row_probe_does_not(sp, tmp_path: Path) -> None:
    from primer.artifact.filesystem import ROOT_MARKER

    saved = await probe_artifact_provider(_fs_row(tmp_path / "new"), storage_provider=sp)
    assert (saved.ok, saved.step) == (False, "initialize") and "does not exist in this process" in saved.error
    assert not (tmp_path / "new").exists(), "a probe without prepare never creates the root"

    draft = await probe_artifact_provider(_fs_row(tmp_path / "new"), storage_provider=sp, prepare=True)

    assert draft == ArtifactProbeResult(ok=True)
    assert (tmp_path / "new" / ROOT_MARKER).is_file()
    assert [p for p in (tmp_path / "new").rglob("*") if p.is_file() and p.name != ROOT_MARKER] == [], "the probe object is gone"


async def test_a_root_that_cannot_be_prepared_names_prepare(sp, tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")

    result = await probe_artifact_provider(_fs_row(tmp_path / "f"), storage_provider=sp, prepare=True)

    assert (result.ok, result.step) == (False, "prepare")
    assert "is not a directory" in result.error


async def test_prepare_on_a_database_row_is_a_plain_probe(sp) -> None:
    assert await probe_artifact_provider(_row(), storage_provider=sp, prepare=True) == ArtifactProbeResult(ok=True)
