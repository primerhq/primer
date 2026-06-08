"""Direct unit tests for the ``_search`` toolset error paths.

The end-to-end happy-path is covered by
``tests/api/test_internal_collections.py``; this file targets the
error wrappers (validation / not-found / generic provider error /
subsystem inactive) which can't be exercised cheaply through the
HTTP surface.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.model.except_ import ConfigError, PrimerError, NotFoundError
from primer.toolset.search import (
    SEARCH_TOOLSET_ID,
    build_search_toolset,
)


@pytest.fixture
def stub_subsystem():
    sub = MagicMock()
    sub.search = AsyncMock()
    return sub


@pytest.fixture
def toolset(stub_subsystem):
    return build_search_toolset(stub_subsystem)


class TestSearchToolsetErrors:
    @pytest.mark.asyncio
    async def test_validation_error_on_empty_query(self, toolset) -> None:
        result = await toolset.call(
            tool_name="search_agents", arguments={"query": ""}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "validation-error"

    @pytest.mark.asyncio
    async def test_subsystem_inactive_propagates(
        self, toolset, stub_subsystem
    ) -> None:
        stub_subsystem.search = AsyncMock(
            side_effect=ConfigError("not bootstrapped yet")
        )
        result = await toolset.call(
            tool_name="search_agents", arguments={"query": "x"}
        )
        assert result.is_error
        body = json.loads(result.output)
        assert body["type"] == "subsystem-inactive"
        assert "not bootstrapped" in body["message"]

    @pytest.mark.asyncio
    async def test_not_found_propagates(self, toolset, stub_subsystem) -> None:
        stub_subsystem.search = AsyncMock(
            side_effect=NotFoundError("collection missing")
        )
        result = await toolset.call(
            tool_name="search_agents", arguments={"query": "x"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "not-found"

    @pytest.mark.asyncio
    async def test_storage_error_propagates(
        self, toolset, stub_subsystem
    ) -> None:
        stub_subsystem.search = AsyncMock(
            side_effect=PrimerError("backend down")
        )
        result = await toolset.call(
            tool_name="search_agents", arguments={"query": "x"}
        )
        assert result.is_error
        assert json.loads(result.output)["type"] == "storage-error"


class TestCatalog:
    @pytest.mark.asyncio
    async def test_toolset_id_and_tool_count(self, toolset) -> None:
        names = [t.id async for t in toolset.list_tools()]
        assert sorted(names) == sorted([
            "search_agents",
            "search_graphs",
            "search_collections",
            "search_tools",
            "search_ai_docs",
        ])
        async for tool in toolset.list_tools():
            assert tool.toolset_id == SEARCH_TOOLSET_ID


class TestDescriptions:
    @pytest.mark.asyncio
    async def test_search_tools_conform(self, toolset) -> None:
        from tests.toolset._desc_conformance import assert_tool_conforms
        async for tool in toolset.list_tools():
            assert_tool_conforms(tool)
