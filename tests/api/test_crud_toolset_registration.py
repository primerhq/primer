"""S5 P1: ``crud`` is a reserved, always-on internal toolset."""
from __future__ import annotations

from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.toolset.crud import CRUD_TOOL_NAMES


def test_crud_is_a_reserved_toolset_id() -> None:
    assert "crud" in RESERVED_TOOLSET_IDS


async def test_registry_resolves_crud_without_a_storage_row(app) -> None:
    provider = await app.state.provider_registry.get_toolset("crud")
    ids = {t.id async for t in provider.list_tools()}
    assert ids == set(CRUD_TOOL_NAMES)


async def test_reserved_toolsets_are_immune_to_invalidation(app) -> None:
    before = await app.state.provider_registry.get_toolset("crud")
    await app.state.provider_registry.invalidate_toolset("crud")
    after = await app.state.provider_registry.get_toolset("crud")
    assert before is after
