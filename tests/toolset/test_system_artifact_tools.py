"""System tools for artifact-storage probes and active-store operations."""

from primer.mcp.safety import is_exposable
from primer.model.provider import ArtifactStorageProvider
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401


async def test_the_test_tool_reports_ok_for_the_database_store(world) -> None:
    sp, toolset, _ = world
    await sp.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider(id="db-t", provider="db", config={}),
    )

    is_error, answer = await _call(
        toolset, "test_artifact_storage_provider", id="db-t",
    )

    assert not is_error
    assert answer == {"ok": True}


async def test_the_test_tool_of_an_unknown_id_is_not_found(world) -> None:
    _, toolset, _ = world

    is_error, answer = await _call(
        toolset, "test_artifact_storage_provider", id="missing",
    )

    assert is_error
    assert answer["type"] == "not-found"


async def test_the_artifact_tools_are_exposable_over_mcp(world) -> None:
    _, toolset, _ = world
    tools = {tool.id: tool async for tool in toolset.list_tools()}

    tool = tools["test_artifact_storage_provider"]
    assert toolset.required_role(tool.id) == "admin"
    assert is_exposable(tool, provider=toolset) == (True, None)
