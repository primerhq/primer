"""M5 non-vacuous schema/core-kit prompt budget.

Measured in-test from the live objects (never hardcoded copies): the three
fixed MCP meta-tools' outer schemas + descriptions, and the five
``CORE_KIT_TOOLS`` descriptors through the ``tool_catalog`` entry seam. The
caps are ``ceil(measured_at_head * 1.2 / 256) * 256`` pinned as literals at
implementation, so a descriptor growth beyond the reviewed head is a
budget breach, not a silent re-derivation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import pytest_asyncio

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.mcp.meta_tools import META_TOOL_DESCRIPTIONS, META_TOOL_SCHEMAS
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.core_kit import CORE_KIT_TOOLS
from primer.toolset.tool_catalog import collect_tool_entries

# Pinned at implementation (head of feat/tools-universal-m5):
# measured meta_bytes = 1316, kit_bytes = 6447.
META_BUDGET = 1792  # ceil(1316 * 1.2 / 256) * 256
KIT_BUDGET = 7936  # ceil(6447 * 1.2 / 256) * 256
KIT_FLOOR = 2048
META_TOOL_NAMES = ("list_tools", "find_tools", "call_tool")


@pytest_asyncio.fixture
async def kit_entries() -> AsyncIterator[dict[str, object]]:
    with TemporaryDirectory() as temporary:
        storage = SqliteStorageProvider(SqliteConfig(path=Path(temporary) / "budget.sqlite"))
        await storage.initialize()
        registry = ProviderRegistry(
            storage,
            llm_factory=lambda provider: object(),
            embedder_factory=lambda provider: object(),
            cross_encoder_factory=lambda provider: object(),
            toolset_factory=lambda provider: object(),
        )
        try:
            app = create_test_app(storage_provider=storage, provider_registry=registry)
            entries = await collect_tool_entries(
                app.state.provider_registry.builtin_toolset_providers()
            )
            yield {entry.scoped_id: entry for entry in entries}
        finally:
            await storage.aclose()


def test_the_mcp_wire_advertises_exactly_the_three_meta_tools() -> None:
    assert tuple(sorted(META_TOOL_SCHEMAS)) == tuple(sorted(META_TOOL_NAMES))
    assert tuple(sorted(META_TOOL_DESCRIPTIONS)) == tuple(sorted(META_TOOL_NAMES))
    # The meta tools carry fixed outer schemas: call_tool takes the scoped id + arguments only.
    assert META_TOOL_SCHEMAS["call_tool"]["required"] == ["scoped_id"]
    assert set(META_TOOL_SCHEMAS["call_tool"]["properties"]) == {"scoped_id", "arguments"}


def test_the_meta_tool_prompt_bytes_are_within_the_pinned_budget() -> None:
    meta_bytes = (
        len(json.dumps(META_TOOL_SCHEMAS, sort_keys=True).encode())
        + sum(len(description.encode()) for description in META_TOOL_DESCRIPTIONS.values())
    )
    assert meta_bytes > 0, "a gutted meta-tool surface cannot pass the budget"
    assert meta_bytes <= META_BUDGET, f"meta prompt bytes {meta_bytes} exceed the pinned budget {META_BUDGET}"


def test_the_core_kit_prompt_bytes_are_within_the_pinned_budget(kit_entries) -> None:
    kit_bytes = 0
    for scoped_id in CORE_KIT_TOOLS:
        entry = kit_entries[scoped_id]
        kit_bytes += len(entry.tool.description.encode())
        kit_bytes += len(json.dumps(entry.tool.args_schema, sort_keys=True).encode())
    assert kit_bytes > 0
    assert kit_bytes >= KIT_FLOOR, f"kit prompt bytes {kit_bytes} below the non-vacuity floor {KIT_FLOOR}"
    assert kit_bytes <= KIT_BUDGET, f"kit prompt bytes {kit_bytes} exceed the pinned budget {KIT_BUDGET}"


def test_the_core_kit_is_exactly_five_tools_spanning_system_and_misc(kit_entries) -> None:
    assert len(CORE_KIT_TOOLS) == 5
    assert len(set(CORE_KIT_TOOLS)) == 5
    toolsets = {scoped_id.split("__", 1)[0] for scoped_id in CORE_KIT_TOOLS}
    assert toolsets == {"system", "misc"}
    for scoped_id in CORE_KIT_TOOLS:
        assert scoped_id in kit_entries, f"{scoped_id} is not a live tool"


def test_generated_route_tools_never_enter_the_core_kit(kit_entries) -> None:
    """Discoverability of the other tools is through the meta-tools only, never the kit."""
    from primer.bootstrap.operator_defaults import SEEDED_AGENT_FACTORIES

    generated_ids = {
        scoped_id for scoped_id, entry in kit_entries.items()
        if entry.tool.operation is not None
    }
    assert len(generated_ids) == 261
    assert set(CORE_KIT_TOOLS).isdisjoint(generated_ids)
    for factory in SEEDED_AGENT_FACTORIES.values():
        assert set(factory("p").tools).isdisjoint(generated_ids)
