"""Migration 32 preserves legacy MCP allowlist semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ConfigDict

from primer.model.common import Identifiable
from primer.storage.migrations import MIGRATIONS
from primer.storage.migrations.m032_mcp_exposure_mode import M032McpExposureMode
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider


class McpExposure(Identifiable):  # noqa: N801 - selects the production table
    model_config = ConfigDict(extra="allow")

    enabled: bool
    allowed_tools: list[str]
    updated_at: datetime


@pytest.fixture
async def sp(tmp_path: Path) -> AsyncIterator[SqliteStorageProvider]:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "data.sqlite"))
    await provider.initialize()
    try:
        yield provider
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_m032_adds_allowlist_mode_and_preserves_unknown_fields(
    sp,
) -> None:
    storage = sp.get_storage(McpExposure)
    await storage.create(
        McpExposure(
            id="singleton",
            enabled=True,
            allowed_tools=["misc__uuid_v4"],
            updated_at=datetime.now(timezone.utc),
            future_field={"kept": True},
        )
    )

    migration = M032McpExposureMode()
    await migration.apply(sp)
    row = await storage.get("singleton")

    assert row is not None
    assert row.model_extra["mode"] == "allowlist"
    assert row.model_extra["future_field"] == {"kept": True}
    await migration.apply(sp)
    assert (await storage.get("singleton")).model_extra["mode"] == "allowlist"


def test_m032_is_registered_as_next_free_migration() -> None:
    assert isinstance(MIGRATIONS[31], M032McpExposureMode)
    assert MIGRATIONS[31].version == 32
