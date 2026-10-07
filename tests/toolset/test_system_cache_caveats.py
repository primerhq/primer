"""The tool descriptors of the entities whose warm adapters a tool edit does NOT refresh say so (task 01a111d1, D5 phase 2c, option 3).

An edit through the REST route drops the warm adapter cached for an artifact-storage provider, a channel provider or a channel
(``ArtifactStorageRegistry.invalidate`` / ``ChannelRegistry.invalidate``), and re-warms a newly chat-enabled channel. The system tools
cannot do the same: those registries invalidate only the cache of the PROCESS they live in, the warm inbound adapters live in the API
process, and a tool runs in whichever process executes the session (in the k3s split, a worker). Mirroring the REST hooks in the tools
would look like parity and do nothing where it matters, so the lead ruled to document the gap instead (and to ticket the bus binding,
01a11358). An agent reading a descriptor must therefore learn that its edit takes effect after the next restart or after the row is
edited through the REST route, and why.

Negative controls: the entities whose invalidation IS cluster-wide (the provider registry publishes on the invalidation bus), and an
entity with no cache at all, must not carry the caveat.
"""

from __future__ import annotations

import pytest

from tests.toolset.test_system_crud_guards import world  # noqa: F401  (world is a fixture)

AFFECTED = ["artifact_storage_provider", "channel_provider", "channel"]


async def _description(toolset, tool_id: str) -> str:
    descriptions = {tool.id: tool.description async for tool in toolset.list_tools()}
    return descriptions[tool_id]


@pytest.mark.parametrize("verb", ["update", "delete"])
@pytest.mark.parametrize("entity", AFFECTED)
@pytest.mark.asyncio
async def test_a_tool_edit_says_it_does_not_refresh_the_warm_adapter(world, entity, verb) -> None:
    _, toolset, _ = world

    text = await _description(toolset, f"{verb}_{entity}")

    assert "does NOT" in text, "the descriptor does not say the warm adapter is left alone"
    assert "restart" in text and "REST route" in text, "the descriptor does not say when the change takes effect"
    assert "process" in text, "the descriptor does not give the reason (the invalidation is local to one process)"


@pytest.mark.parametrize(
    "tool_id",
    ["update_llm_provider", "delete_llm_provider", "update_toolset", "update_embedding_provider", "update_agent", "delete_agent"],
)
@pytest.mark.asyncio
async def test_other_tools_carry_no_such_caveat(world, tool_id) -> None:
    _, toolset, _ = world

    text = await _description(toolset, tool_id)

    assert "restart" not in text, f"{tool_id} refreshes its adapter (or has none), so the warm-adapter caveat does not apply"
