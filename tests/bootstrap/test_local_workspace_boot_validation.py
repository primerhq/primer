"""The boot refuses a default workspace template on a local provider the deployment refuses (ticket 01a1072f).

Uses the production ``create_app`` lifespan over a real SQLite database, so the validation is exercised where it is wired:
after bootstrap has written the reserved ``local`` provider and ``local-default`` template, and BEFORE the ensure pass
(which swallows a step's error and would only log that the default workspace could not be seeded).
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.except_ import ConfigError
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.model.scheduler import RuntimeMode


def _config(db_path: Path, **overrides) -> AppConfig:
    return AppConfig(
        runtime_mode=overrides.pop("runtime_mode", RuntimeMode.API),
        db=StorageProviderConfig(provider=StorageProviderType.SQLITE, config=SqliteConfig(path=db_path)),
        **overrides,
    )


async def test_boot_fails_when_the_default_template_is_on_a_refused_local_provider(tmp_path) -> None:
    app = create_app(_config(tmp_path / "db.sqlite", local_workspaces={"refuse_when_distributed": True}))
    try:
        with pytest.raises(ConfigError, match="default_workspace_template 'local-default'"):
            async with app.router.lifespan_context(app):
                pytest.fail("the app must not start")
    finally:
        # A boot that fails leaves the storage provider it opened open (the process exits); a test's process does not.
        await app.state.storage_provider.aclose()


async def test_boot_succeeds_with_the_switch_off_even_when_distributed(tmp_path, caplog) -> None:
    app = create_app(_config(tmp_path / "db.sqlite"))
    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                local = (await client.get("/v1/health")).json()["workspaces"]["local"]
    assert local["distributed"] is True and local["enforcing"] is False and local["refusing_local"] is False
    assert any("refuse_when_distributed is off" in r.getMessage() for r in caplog.records)


async def test_boot_succeeds_when_the_deployment_is_single_process(tmp_path) -> None:
    app = create_app(_config(
        tmp_path / "db.sqlite", runtime_mode=RuntimeMode.API_PLUS_WORKER,
        local_workspaces={"refuse_when_distributed": True},
    ))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            local = (await client.get("/v1/health")).json()["workspaces"]["local"]
    assert local["distributed"] is False and local["enforcing"] is True and local["refusing_local"] is False
