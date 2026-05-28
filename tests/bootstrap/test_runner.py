"""Tests for BootstrapRunner — idempotent first-boot provider creation.

Uses the real SQLite storage backend so we exercise actual persistence.
Each test gets an isolated tmp_path DB; no shared state between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from matrix.bootstrap.defaults import (
    RESERVED_HUGGINGFACE_CROSS_ENCODER,
    RESERVED_HUGGINGFACE_EMBEDDER,
    RESERVED_LANCE_SSP,
    RESERVED_LOCAL_WORKSPACE_PROVIDER,
)
from matrix.bootstrap.runner import BootstrapResult, BootstrapRunner
from matrix.int.storage import Storage
from matrix.model.common import Identifiable
from matrix.model.except_ import ConflictError, NotFoundError
from matrix.model.provider import SqliteConfig
from matrix.model.system_state import SystemState
from matrix.storage.sqlite import SqliteStorageProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def storage_provider(
    tmp_path: Path,
) -> AsyncIterator[SqliteStorageProvider]:
    """Fresh SQLite-backed storage provider per test."""
    cfg = SqliteConfig(path=tmp_path / "matrix.sqlite")
    provider = SqliteStorageProvider(cfg)
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.fixture
def root_dir(tmp_path: Path) -> Path:
    """Isolated root directory for provider file paths."""
    d = tmp_path / "matrix_root"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_runner(
    storage: SqliteStorageProvider,
    root: Path,
    *,
    embedder_storage: Storage | None = None,
) -> BootstrapRunner:
    """Build a BootstrapRunner, optionally overriding the embedder storage."""
    return BootstrapRunner(
        storage=storage,
        embedder_storage=embedder_storage or storage.get_storage(
            __import__(
                "matrix.model.provider", fromlist=["EmbeddingProvider"]
            ).EmbeddingProvider
        ),
        ssp_storage=storage.get_storage(
            __import__(
                "matrix.model.provider", fromlist=["SemanticSearchProvider"]
            ).SemanticSearchProvider
        ),
        cross_encoder_storage=storage.get_storage(
            __import__(
                "matrix.model.provider", fromlist=["CrossEncoderProvider"]
            ).CrossEncoderProvider
        ),
        workspace_provider_storage=storage.get_storage(
            __import__(
                "matrix.model.workspace", fromlist=["WorkspaceProvider"]
            ).WorkspaceProvider
        ),
        root_dir=root,
    )


# ---------------------------------------------------------------------------
# Broken storage helper
# ---------------------------------------------------------------------------


class _BrokenEmbedderStorage:
    """Storage stub whose create() always raises RuntimeError."""

    async def get(self, id: str) -> None:  # noqa: A002
        return None  # acts like absent so we reach create()

    async def create(self, entity: Any) -> Any:
        raise RuntimeError("simulated embedder storage failure")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_needs_bootstrap_true_on_fresh_db(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """needs_bootstrap() must be True on a newly-initialised DB."""
    runner = _make_runner(storage_provider, root_dir)
    assert await runner.needs_bootstrap() is True


@pytest.mark.asyncio
async def test_run_creates_all_four_providers(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """First run must create all four reserved providers and stamp the marker."""
    runner = _make_runner(storage_provider, root_dir)
    result = await runner.run()

    assert set(result.created) == {
        RESERVED_LOCAL_WORKSPACE_PROVIDER,
        RESERVED_HUGGINGFACE_EMBEDDER,
        RESERVED_LANCE_SSP,
        RESERVED_HUGGINGFACE_CROSS_ENCODER,
    }
    assert result.skipped == []
    assert result.errors == []

    state = await storage_provider.get_system_state()
    assert state.bootstrap_completed_at is not None


@pytest.mark.asyncio
async def test_run_idempotent_skips_existing(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """Second run after a successful first run must skip all four providers."""
    runner = _make_runner(storage_provider, root_dir)

    first = await runner.run()
    assert first.errors == []

    second = await runner.run()
    assert second.created == []
    assert set(second.skipped) == {
        RESERVED_LOCAL_WORKSPACE_PROVIDER,
        RESERVED_HUGGINGFACE_EMBEDDER,
        RESERVED_LANCE_SSP,
        RESERVED_HUGGINGFACE_CROSS_ENCODER,
    }
    assert second.errors == []


@pytest.mark.asyncio
async def test_needs_bootstrap_false_after_success(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """needs_bootstrap() must be False once the marker is stamped."""
    runner = _make_runner(storage_provider, root_dir)
    result = await runner.run()
    assert result.errors == []
    assert await runner.needs_bootstrap() is False


@pytest.mark.asyncio
async def test_partial_failure_leaves_marker_null(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """If any _ensure_* step raises, bootstrap_completed_at stays null."""
    broken = _BrokenEmbedderStorage()
    runner = _make_runner(storage_provider, root_dir, embedder_storage=broken)

    result = await runner.run()

    # The huggingface embedder ensure must have errored.
    assert any(
        rid == RESERVED_HUGGINGFACE_EMBEDDER
        for rid, _ in result.errors
    ), f"Expected huggingface error; got {result.errors}"

    # Marker must NOT have been stamped.
    state = await storage_provider.get_system_state()
    assert state.bootstrap_completed_at is None


@pytest.mark.asyncio
async def test_partial_failure_other_ensures_still_run(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """A failure in one _ensure_* must not prevent the others from running."""
    broken = _BrokenEmbedderStorage()
    runner = _make_runner(storage_provider, root_dir, embedder_storage=broken)

    result = await runner.run()

    # All three non-broken providers should have been created.
    assert RESERVED_HUGGINGFACE_EMBEDDER not in result.created
    for rid in (
        RESERVED_LOCAL_WORKSPACE_PROVIDER,
        RESERVED_LANCE_SSP,
        RESERVED_HUGGINGFACE_CROSS_ENCODER,
    ):
        assert rid in result.created or rid in result.skipped, (
            f"{rid} not in created or skipped"
        )


@pytest.mark.asyncio
async def test_run_with_force_reruns_after_completion(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """run(force=True) ignores the marker and re-runs even if already done."""
    runner = _make_runner(storage_provider, root_dir)

    first = await runner.run()
    assert first.errors == []
    assert await runner.needs_bootstrap() is False

    # Second run with force — marker is set but force=True re-runs.
    # All four already exist, so they should all be skipped (not re-created).
    second = await runner.run(force=True)
    assert second.created == []
    assert set(second.skipped) == {
        RESERVED_LOCAL_WORKSPACE_PROVIDER,
        RESERVED_HUGGINGFACE_EMBEDDER,
        RESERVED_LANCE_SSP,
        RESERVED_HUGGINGFACE_CROSS_ENCODER,
    }
    assert second.errors == []


@pytest.mark.asyncio
async def test_run_no_op_when_marker_set_and_no_force(
    storage_provider: SqliteStorageProvider,
    root_dir: Path,
) -> None:
    """run() without force is a no-op when the marker is already set."""
    runner = _make_runner(storage_provider, root_dir)

    await runner.run()  # First run stamps the marker.
    result = await runner.run()  # Second run — marker set, no force.

    # Should skip everything (no new creates, no errors).
    assert result.created == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_bootstrap_result_dataclass() -> None:
    """BootstrapResult is a plain dataclass with the expected fields."""
    r = BootstrapResult(created=["a"], skipped=["b"], errors=[("c", "reason")])
    assert r.created == ["a"]
    assert r.skipped == ["b"]
    assert r.errors == [("c", "reason")]
