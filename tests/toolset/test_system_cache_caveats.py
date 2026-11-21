"""The system tools describe asynchronous channel listener reconciliation."""

from __future__ import annotations

import pytest

from tests.toolset.test_system_crud_guards import world  # noqa: F401  (world is a fixture)

AFFECTED = ["channel_provider", "channel"]


async def _description(toolset, tool_id: str) -> str:
    descriptions = {tool.id: tool.description async for tool in toolset.list_tools()}
    return descriptions[tool_id]


@pytest.mark.parametrize("verb", ["create", "update", "delete"])
@pytest.mark.parametrize("entity", AFFECTED)
@pytest.mark.asyncio
async def test_channel_write_explains_listener_ownership_and_retries(world, entity, verb) -> None:
    _, toolset, _ = world

    text = await _description(toolset, f"{verb}_{entity}")

    assert "owns inbound" in text and "saved channel" in text
    assert "starting or retrying" in text


@pytest.mark.parametrize(
    "tool_id",
    [
        "update_llm_provider", "delete_llm_provider", "update_toolset", "update_embedding_provider", "update_agent", "delete_agent",
        "update_artifact_storage_provider", "delete_artifact_storage_provider",
    ],
)
@pytest.mark.asyncio
async def test_other_tools_carry_no_such_caveat(world, tool_id) -> None:
    _, toolset, _ = world

    text = await _description(toolset, tool_id)

    assert "restart" not in text, f"{tool_id} refreshes its adapter (or has none), so the warm-adapter caveat does not apply"
