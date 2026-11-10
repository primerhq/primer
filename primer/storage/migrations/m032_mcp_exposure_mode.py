"""Migration 32: make legacy MCP exposure narrowing explicit."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable


class McpExposure(Identifiable):  # noqa: N801 - existing table name
    """Migration-local view preserving fields unknown to this migration."""

    model_config = ConfigDict(extra="allow")

    mode: Literal["allowlist", "rbac"] = "allowlist"


class M032McpExposureMode:
    """Stamp an upgraded singleton with its historical allowlist behavior."""

    version = 32
    description = "preserve legacy MCP exposure rows in allowlist mode"

    async def apply(self, sp: StorageProvider) -> None:
        storage = sp.get_storage(McpExposure)
        row = await storage.get("singleton")
        if row is None or "mode" in (row.model_fields_set or set()):
            return
        await storage.update(row.model_copy(update={"mode": "allowlist"}))


__all__ = ["M032McpExposureMode"]
