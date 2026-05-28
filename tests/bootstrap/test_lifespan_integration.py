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

import httpx
import pytest
import pytest_asyncio

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


@asynccontextmanager
async def _boot_app(
    *, auto_bootstrap: bool, db_path: Path
) -> AsyncGenerator[_AppHandle, None]:
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
            yield _AppHandle(client=client)


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
