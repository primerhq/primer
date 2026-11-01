"""GX-2: a harness sync or uninstall evicts the cached toolset adapter, and a toolset whose row is gone is not available (never built-in)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.api.registries.provider_registry import ProviderRegistry
from primer.harness.service import apply_install, apply_sync, apply_uninstall
from primer.model.except_ import NotFoundError
from primer.model.harness import Harness, RenderedEntry
from primer.model.principal import PrincipalRef
from primer.model.provider import Toolset
from primer.storage.sqlite import SqliteConfig, SqliteStorageProvider

pytestmark = pytest.mark.asyncio

_ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")


def _payload(url: str) -> dict:
    return {"provider": "mcp", "config": {"transport": "http", "config": {"url": url}}}


def _entry(url: str, rendered_hash: str) -> RenderedEntry:
    return RenderedEntry(
        kind="toolset", template_name="ts", resolved_id="acme__ts",
        template_source_hash="h", rendered_hash=rendered_hash, rendered_payload=_payload(url),
    )


class _Adapter:
    def __init__(self, toolset: Toolset) -> None:
        self.url = str(toolset.config.config.url)

    async def aclose(self) -> None:
        return None


async def _world(tmp_path):
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "h.sqlite")))
    await sp.initialize()
    harness = Harness(
        id="acme-id", slug="acme", name="Acme", git_url="https://github.com/x/y",
        operation_requested_by=_ADMIN, created_at=datetime.now(timezone.utc),
    )
    return sp, harness, ProviderRegistry(sp, toolset_factory=_Adapter)


async def _install(sp, harness, registry, url="https://old.example/mcp"):
    return await apply_install(
        storage_provider=sp, harness=harness, entries=[_entry(url, "r1")], rendered_files_by_name={},
        bundle_hash="b1", overrides_hash="o1", schema_hash=None, provider_registry=registry,
    )


async def test_a_sync_that_moves_the_mcp_url_is_seen_by_the_next_request(tmp_path):
    sp, harness, registry = await _world(tmp_path)
    try:
        assert await _install(sp, harness, registry) is None
        assert (await registry.get_toolset("acme__ts")).url == "https://old.example/mcp"
        assert await apply_sync(
            storage_provider=sp, harness=harness, new_entries=[_entry("https://new.example/mcp", "r2")],
            rendered_files_by_name={}, bundle_hash="b2", overrides_hash="o1", schema_hash=None, provider_registry=registry,
        ) is None
        assert (await registry.get_toolset("acme__ts")).url == "https://new.example/mcp"
    finally:
        await sp.aclose()


async def test_an_uninstall_stops_serving_the_deleted_toolset(tmp_path):
    sp, harness, registry = await _world(tmp_path)
    try:
        assert await _install(sp, harness, registry) is None
        assert (await registry.get_toolset("acme__ts")).url == "https://old.example/mcp"
        await apply_uninstall(storage_provider=sp, harness=harness, provider_registry=registry)
        assert await sp.get_storage(Toolset).get("acme__ts") is None
        with pytest.raises(NotFoundError):
            await registry.get_toolset("acme__ts")
    finally:
        await sp.aclose()


async def test_a_sync_that_drops_a_toolset_evicts_it_too(tmp_path):
    sp, harness, registry = await _world(tmp_path)
    try:
        assert await _install(sp, harness, registry) is None
        await registry.get_toolset("acme__ts")
        assert await apply_sync(
            storage_provider=sp, harness=harness, new_entries=[], rendered_files_by_name={},
            bundle_hash="b2", overrides_hash="o1", schema_hash=None, provider_registry=registry,
        ) is None
        with pytest.raises(NotFoundError):
            await registry.get_toolset("acme__ts")
    finally:
        await sp.aclose()


async def test_an_install_without_a_registry_still_works(tmp_path):
    sp, harness, _ = await _world(tmp_path)
    try:
        assert await apply_install(
            storage_provider=sp, harness=harness, entries=[_entry("https://x.example/mcp", "r1")], rendered_files_by_name={},
            bundle_hash="b1", overrides_hash="o1", schema_hash=None,
        ) is None
        await apply_uninstall(storage_provider=sp, harness=harness)
    finally:
        await sp.aclose()
