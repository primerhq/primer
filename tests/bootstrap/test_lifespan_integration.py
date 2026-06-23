"""Integration tests for first-boot auto-bootstrap lifespan wiring.

Uses a real SQLite storage provider + the production ``create_app``
factory (with its full lifespan) so we exercise the actual bootstrap
path end-to-end. Each test gets its own isolated tmp-path database;
there is no shared state between tests.

Subsystems exercised:
- AppConfig.auto_bootstrap knob (default True)
- Lifespan: BootstrapRunner.needs_bootstrap() check + run()
- Lifespan: warning log when auto_bootstrap=False on first boot
- Idempotency: second boot with same DB does NOT re-run bootstrap
- GET /v1/embedding_providers reflects created reserved provider
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncio

import httpx
import pytest
import pytest_asyncio

from fastapi import FastAPI

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.scheduler import RuntimeMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(*, auto_bootstrap: bool, db_path: Path) -> AppConfig:
    """Build an AppConfig with a real SQLite path and no worker pool."""
    return AppConfig(
        runtime_mode=RuntimeMode.API,
        auto_bootstrap=auto_bootstrap,
        db=StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=db_path),
        ),
    )


@dataclass
class _AppHandle:
    """Thin wrapper exposing ``client`` for use inside the ``async with`` block."""

    client: httpx.AsyncClient
    app: FastAPI


@asynccontextmanager
async def _boot_app(
    *, auto_bootstrap: bool, db_path: Path
) -> AsyncGenerator[_AppHandle]:
    """Context manager: boot the real app lifespan + an httpx client.

    Tears down cleanly on exit regardless of test outcome.
    """
    cfg = _make_cfg(auto_bootstrap=auto_bootstrap, db_path=db_path)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            # Auto-register (first boot) or login (subsequent) so
            # auth-protected endpoints respond.
            try:
                r = await client.post(
                    "/v1/auth/register",
                    json={"username": "testuser", "password": "testpassword"},
                )
                if r.status_code == 409:
                    await client.post(
                        "/v1/auth/login",
                        json={"username": "testuser", "password": "testpassword"},
                    )
            except Exception:
                pass
            yield _AppHandle(client=client, app=app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """A unique SQLite file path inside pytest's tmp_path."""
    return tmp_path / "bootstrap_test.sqlite"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_boot_auto_bootstraps(tmp_db_path: Path) -> None:
    """Fresh DB + auto_bootstrap=True: reserved providers visible via GET."""
    async with _boot_app(auto_bootstrap=True, db_path=tmp_db_path) as handle:
        resp = await handle.client.get("/v1/embedding_providers")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        ids = [e["id"] for e in data.get("items", [])]
        assert "huggingface" in ids, (
            f"Expected 'huggingface' in embedding_providers; got ids={ids}"
        )


@pytest.mark.asyncio
async def test_fresh_boot_with_opt_out_skips(
    tmp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fresh DB + auto_bootstrap=False: no providers created; warning logged."""
    with caplog.at_level(logging.WARNING, logger="primer.api.app"):
        async with _boot_app(auto_bootstrap=False, db_path=tmp_db_path) as handle:
            resp = await handle.client.get("/v1/embedding_providers")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data.get("items", []) == [], (
                f"Expected no embedding providers on opt-out boot; got {data}"
            )

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("auto_bootstrap=False" in msg for msg in warning_messages), (
        f"Expected auto_bootstrap=False warning; got warning messages: {warning_messages}"
    )


@pytest.mark.asyncio
async def test_second_boot_does_not_re_bootstrap(
    tmp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Second boot with same DB: bootstrap does NOT re-run (marker already set)."""
    # First boot: bootstrap runs.
    async with _boot_app(auto_bootstrap=True, db_path=tmp_db_path):
        pass

    # Second boot: marker already set, should NOT log "running auto-bootstrap".
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="primer.api.app"):
        async with _boot_app(auto_bootstrap=True, db_path=tmp_db_path) as handle:
            # Providers are still there from first boot.
            resp = await handle.client.get("/v1/embedding_providers")
            assert resp.status_code == 200
            ids = [e["id"] for e in resp.json().get("items", [])]
            assert "huggingface" in ids

    bootstrap_log_messages = [r.getMessage() for r in caplog.records]
    assert all("running auto-bootstrap" not in msg for msg in bootstrap_log_messages), (
        f"Second boot should NOT re-run bootstrap; found in logs: "
        f"{[m for m in bootstrap_log_messages if 'running auto-bootstrap' in m]}"
    )


@pytest.mark.asyncio
async def test_lifespan_starts_workspace_probe(tmp_db_path: Path) -> None:
    """Lifespan spawns the WorkspaceProbeTask runner and stops it cleanly."""
    async with _boot_app(auto_bootstrap=True, db_path=tmp_db_path) as handle:
        probe = getattr(handle.app.state, "workspace_probe", None)
        runner = getattr(handle.app.state, "workspace_probe_runner", None)
        assert probe is not None, "expected app.state.workspace_probe to be set"
        assert runner is not None, (
            "expected app.state.workspace_probe_runner to be set"
        )
        assert isinstance(runner, asyncio.Task)
        assert not runner.done(), "probe runner should be active during lifespan"


@pytest.mark.asyncio
async def test_lifespan_recovers_running_session_into_claim_engine(
    tmp_db_path: Path,
) -> None:
    """A RUNNING WorkspaceSession row persisted before this process must
    be re-armed in the claim engine on startup. This is Bug 1 from the
    diagnostic report: without this, sessions created in a prior api
    process are invisible to the worker pool forever after a restart.
    """
    from datetime import datetime, timezone

    from primer.int.claim import ClaimKind
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    # Recovery needs a claim engine — that requires a scheduler. Use
    # API-only mode + an explicit scheduler config so the engine is
    # wired but no worker pool runs (otherwise the worker would claim
    # the recovered lease immediately and drop it on the workspace
    # lookup failure, defeating the assertion below).
    from primer.model.scheduler import (
        InMemorySchedulerConfig,
        SchedulerProviderConfig,
        SchedulerProviderType,
    )

    def _cfg() -> AppConfig:
        return AppConfig(
            runtime_mode=RuntimeMode.API,
            auto_bootstrap=True,
            scheduler=SchedulerProviderConfig(
                provider=SchedulerProviderType.IN_MEMORY,
                config=InMemorySchedulerConfig(),
            ),
            db=StorageProviderConfig(
                provider=StorageProviderType.SQLITE,
                config=SqliteConfig(path=tmp_db_path),
            ),
        )

    # ---- Boot #1: seed a RUNNING session row directly via storage ----
    app1 = create_app(_cfg())
    async with app1.router.lifespan_context(app1):
        sp = app1.state.storage_provider
        storage = sp.get_storage(WorkspaceSession)
        sess = WorkspaceSession(
            id="sess-recovered",
            workspace_id="ws-from-prior-boot",
            binding=AgentSessionBinding(agent_id="ag-x"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        await storage.create(sess)

    # ---- Boot #2: same DB, the recovery loop should pick up the row ----
    app2 = create_app(_cfg())
    async with app2.router.lifespan_context(app2):
        engine = app2.state.claim_engine
        assert engine is not None, (
            "expected app.state.claim_engine to be wired in API_PLUS_WORKER mode"
        )
        # In-memory engine exposes ._leases as the source of truth; the
        # post-recovery state should include a lease for the row.
        leases = getattr(engine, "_leases", None)
        assert leases is not None, (
            "in-memory claim engine should expose ._leases"
        )
        assert (ClaimKind.SESSION, "sess-recovered") in leases, (
            f"recovered lease not in engine; got keys: {list(leases.keys())}"
        )
