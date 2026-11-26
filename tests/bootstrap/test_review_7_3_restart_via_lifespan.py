"""Review 7.3 (round 1): the real boot phase must create an extra's default rows on a RESTART.

``run_first_boot_bootstrap`` is what the server lifespan calls; it used to call ``run()`` only while the marker was unset, so the marked path
of ``BootstrapRunner.run`` was never reached on a restart.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import primer.common.optional as optional_mod
from primer.api._app_lifespan_phases import run_first_boot_bootstrap
from primer.api.config import AppConfig
from primer.model.provider import (
    CrossEncoderProvider, EmbeddingProvider, SemanticSearchProvider, SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.model.scheduler import RuntimeMode
from primer.storage.sqlite import SqliteStorageProvider


@pytest.mark.asyncio
async def test_installing_an_extra_and_restarting_creates_its_rows_through_the_lifespan_phase(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API, auto_bootstrap=True,
        db=StorageProviderConfig(provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "p.sqlite")),
    )
    sp = SqliteStorageProvider(cfg.db.config)
    await sp.initialize()
    real = optional_mod._find_spec
    try:
        with monkeypatch.context() as slim:
            slim.setattr(optional_mod, "_find_spec", lambda n, *a, **k: None if n in ("sentence_transformers", "lancedb") else real(n, *a, **k))
            await run_first_boot_bootstrap(cfg, sp)
        assert (await sp.get_system_state()).bootstrap_completed_at is not None
        assert await sp.get_storage(EmbeddingProvider).get("huggingface") is None

        # The operator installs the extras and restarts: the marker is set, force is not asked for.
        monkeypatch.setattr(optional_mod, "_find_spec", lambda n, *a, **k: object() if n in ("sentence_transformers", "lancedb") else real(n, *a, **k))
        await run_first_boot_bootstrap(cfg, sp)
        assert await sp.get_storage(EmbeddingProvider).get("huggingface") is not None
        assert await sp.get_storage(SemanticSearchProvider).get("lance") is not None
        assert await sp.get_storage(CrossEncoderProvider).get("huggingface-ce") is not None

        # And the next restart is quiet: nothing is created twice, nothing raises.
        await run_first_boot_bootstrap(cfg, sp)
    finally:
        await sp.aclose()


@pytest.mark.asyncio
async def test_a_marked_boot_converges_the_pgvector_row_and_the_next_writes_nothing(fake_storage_provider, monkeypatch):
    """O-1 addendum: the dropped gate also kept ``_follow_app_settings`` off marked boots. It now runs on every boot and is idempotent:
    the first marked boot converges the reserved pgvector row (the app's rotated password reaches it); the next writes nothing (one read,
    no write: cheap)."""
    from datetime import UTC, datetime

    from pydantic import SecretStr

    from primer.bootstrap.defaults import RESERVED_PGVECTOR_SSP
    from primer.bootstrap.runner import BootstrapRunner
    from primer.model.providers.storage import PostgresConfig, StorageProviderConfig, StorageProviderType
    from primer.model.providers.vector import PgVectorConfig
    from primer.model.workspace import WorkspaceProvider, WorkspaceTemplate

    sp = fake_storage_provider
    real = optional_mod._find_spec
    monkeypatch.setattr(
        optional_mod, "_find_spec",
        lambda n, *a, **k: None if n in ("sentence_transformers", "lancedb") else real(n, *a, **k),
    )
    pg = PostgresConfig(hostname="db.internal", username="app", password=SecretStr("rotated"), database="appdb", db_schema="vecs")
    db_config = StorageProviderConfig(provider=StorageProviderType.POSTGRES, config=pg)
    # The row a first boot wrote: same server, the app's old password and default pool.
    ssp = sp.get_storage(SemanticSearchProvider)
    await ssp.create(SemanticSearchProvider(
        id=RESERVED_PGVECTOR_SSP, provider="pgvector",
        config=PgVectorConfig(hostname="db.internal", username="app", password=SecretStr("old"), database="appdb", db_schema="vecs"),
    ))
    await sp.set_bootstrap_completed(datetime.now(UTC))      # the marker is set: this is a marked boot

    runner = BootstrapRunner(
        storage=sp,
        embedder_storage=sp.get_storage(EmbeddingProvider),
        ssp_storage=ssp,
        cross_encoder_storage=sp.get_storage(CrossEncoderProvider),
        workspace_provider_storage=sp.get_storage(WorkspaceProvider),
        workspace_template_storage=sp.get_storage(WorkspaceTemplate),
        root_dir=Path("/nonexistent-root"),
        db_config=db_config,
        legacy_vector_store=None,
    )
    writes = 0
    original_update = ssp.update

    async def counting_update(*args, **kwargs):
        nonlocal writes
        writes += 1
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(ssp, "update", counting_update)
    first = await runner.run()
    assert writes == 1, "the marked boot converged the row (the app's rotated password reached it)"
    assert (await ssp.get(RESERVED_PGVECTOR_SSP)).config.password.get_secret_value() == "rotated"
    second = await runner.run()
    assert writes == 1, "the next marked boot converged nothing: _follow_app_settings is idempotent and read-only when in step"
    assert second.errors == [] and second.created == []
