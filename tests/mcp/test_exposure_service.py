"""McpExposure model + service -- Spec §6."""

from __future__ import annotations

import pytest

from primer.mcp.exposure import (
    ExposureDeps,
    ToolNotExposable,
    ToolUnknown,
    get_exposure,
    list_available_tools,
    update_exposure,
)
from primer.model.mcp_exposure import McpExposure


def _deps(storage, registry) -> ExposureDeps:
    return ExposureDeps(storage_provider=storage, provider_registry=registry)


@pytest.mark.asyncio
async def test_get_exposure_creates_singleton_on_first_call(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """First read lazily creates the row with the safe default state."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    row = await get_exposure(deps)

    assert isinstance(row, McpExposure)
    assert row.id == "singleton"
    assert row.enabled is False
    assert row.allowed_tools == []
    # Persisted so subsequent gets don't re-create.
    stored = await fake_storage_provider.get_storage(McpExposure).get("singleton")
    assert stored is not None


@pytest.mark.asyncio
async def test_get_exposure_returns_existing(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Second call returns the same singleton, no overwrite."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    first = await get_exposure(deps)
    second = await get_exposure(deps)

    assert second.id == first.id == "singleton"
    assert second.updated_at == first.updated_at
    # Same row, not a duplicate.
    assert second is not None


@pytest.mark.asyncio
async def test_update_exposure_enables(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Flipping ``enabled`` stamps updated_at + updated_by."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    row = await update_exposure(
        enabled=True, allowed_tools=None,
        updated_by="alice", deps=deps,
    )

    assert row.enabled is True
    assert row.updated_by == "alice"
    assert row.allowed_tools == []  # untouched


@pytest.mark.asyncio
async def test_update_exposure_sets_allowed_tools_dedup_sort(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """``allowed_tools`` is deduped and sorted canonically before persist."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    row = await update_exposure(
        enabled=None,
        allowed_tools=["misc__now", "misc__uuid_v4", "misc__uuid_v4"],
        updated_by="bob", deps=deps,
    )

    assert row.allowed_tools == ["misc__now", "misc__uuid_v4"]
    assert row.updated_by == "bob"
    # enabled untouched by this PATCH.
    assert row.enabled is False


@pytest.mark.asyncio
async def test_update_exposure_rejects_unknown_id(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """A scoped id that no toolset emits raises :class:`ToolUnknown`."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    with pytest.raises(ToolUnknown) as excinfo:
        await update_exposure(
            enabled=None,
            allowed_tools=["misc__no_such_tool"],
            updated_by=None, deps=deps,
        )
    assert excinfo.value.scoped_id == "misc__no_such_tool"


@pytest.mark.asyncio
async def test_update_exposure_rejects_non_exposable_id(
    fake_storage_provider, fake_provider_registry_with_yielding,
) -> None:
    """A real-but-yielding tool surfaces :class:`ToolNotExposable`."""
    deps = _deps(
        fake_storage_provider, fake_provider_registry_with_yielding,
    )

    with pytest.raises(ToolNotExposable) as excinfo:
        await update_exposure(
            enabled=None,
            allowed_tools=["misc__uuid_v4"],
            updated_by=None, deps=deps,
        )
    assert excinfo.value.scoped_id == "misc__uuid_v4"
    assert excinfo.value.reason == "yielding_unsupported"


@pytest.mark.asyncio
async def test_list_available_tools_shape(
    fake_storage_provider, fake_provider_registry_with_tools,
) -> None:
    """Every catalogue tool surfaces with the documented enrichment keys."""
    deps = _deps(fake_storage_provider, fake_provider_registry_with_tools)

    # Pre-allow one tool so ``currently_allowed`` flips for it.
    await update_exposure(
        enabled=True, allowed_tools=["misc__uuid_v4"],
        updated_by="alice", deps=deps,
    )

    rows = await list_available_tools(deps)

    assert len(rows) == 2
    by_id = {r["scoped_id"]: r for r in rows}
    expected_keys = {
        "scoped_id", "toolset_id", "description",
        "exposable", "reason", "currently_allowed",
    }
    for r in rows:
        assert set(r.keys()) == expected_keys
        assert r["toolset_id"] == "misc"
        assert r["exposable"] is True
        assert r["reason"] is None

    assert by_id["misc__uuid_v4"]["currently_allowed"] is True
    assert by_id["misc__now"]["currently_allowed"] is False
