"""System tools for artifact-storage probes and active-store operations."""

from primer.mcp.safety import is_exposable
from primer.model.artifact import ArtifactLocation
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

    for tool_id in (
        "test_artifact_storage_provider",
        "get_active_artifact_store",
        "set_active_artifact_store",
    ):
        tool = tools[tool_id]
        assert toolset.required_role(tool.id) == "admin"
        assert is_exposable(tool, provider=toolset) == (True, None)

    assert tools["test_artifact_storage_provider"].interruptible is False
    assert tools["set_active_artifact_store"].interruptible is False
    assert tools["get_active_artifact_store"].interruptible is True


async def test_set_then_get_the_active_store(world) -> None:
    sp, toolset, _ = world
    await sp.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider(id="db-2", provider="db", config={}),
    )

    set_error, selected = await _call(toolset, "set_active_artifact_store", provider_id="db-2")
    get_error, current = await _call(toolset, "get_active_artifact_store")

    assert not set_error and not get_error
    assert selected == current == {"provider_id": "db-2", "provider": "db"}


async def test_setting_an_unknown_store_is_a_validation_error(world) -> None:
    _, toolset, _ = world
    is_error, answer = await _call(toolset, "set_active_artifact_store", provider_id="missing")
    assert is_error
    assert answer["type"] == "validation-error"
    assert answer["message"].startswith("provider_id: ")


async def test_the_active_store_cannot_be_deleted_by_a_tool(world) -> None:
    sp, toolset, _ = world
    await sp.get_storage(ArtifactStorageProvider).create(
        ArtifactStorageProvider(id="db-2", provider="db", config={}),
    )
    assert not (await _call(toolset, "set_active_artifact_store", provider_id="db-2"))[0]

    is_error, answer = await _call(toolset, "delete_artifact_storage_provider", id="db-2")

    assert is_error
    assert answer["type"] == "conflict"


async def test_a_tool_cannot_delete_or_move_a_store_that_holds_artifacts(world, tmp_path) -> None:
    sp, toolset, _ = world
    row = ArtifactStorageProvider(
        id="fs-held",
        provider="filesystem",
        config={"root": str(tmp_path / "a")},
    )
    await sp.get_storage(ArtifactStorageProvider).create(row)
    await sp.get_storage(ArtifactLocation).create(
        ArtifactLocation(id="artifact-held", provider_id="fs-held"),
    )

    delete_error, delete_answer = await _call(
        toolset,
        "delete_artifact_storage_provider",
        id="fs-held",
    )
    moved = row.model_dump(mode="json")
    moved["config"]["root"] = str(tmp_path / "b")
    update_error, update_answer = await _call(
        toolset,
        "update_artifact_storage_provider",
        id="fs-held",
        entity=moved,
    )

    assert delete_error and delete_answer["type"] == "conflict"
    assert "1 artifact(s)" in delete_answer["message"]
    assert update_error and update_answer["type"] == "conflict"
    assert update_answer["message"].startswith("config.root: ")


async def test_a_store_deleted_while_the_tool_checks_it_is_a_conflict(world, monkeypatch) -> None:
    """The G5 audit, G5-06, through the tool: ``type=conflict`` naming the field, and the selection is unchanged."""
    from primer.artifact.probe import ArtifactProbeResult

    sp, toolset, _ = world
    await sp.get_storage(ArtifactStorageProvider).create(ArtifactStorageProvider(id="db-2", provider="db", config={}))

    async def deleted_meanwhile(row, *, storage_provider):
        await storage_provider.get_storage(ArtifactStorageProvider).delete(row.id)
        return ArtifactProbeResult(ok=True)

    monkeypatch.setattr("primer.artifact.active.probe_artifact_provider", deleted_meanwhile)
    is_error, answer = await _call(toolset, "set_active_artifact_store", provider_id="db-2")

    assert is_error and answer["type"] == "conflict"
    assert answer["message"].startswith("provider_id: artifact store 'db-2' was deleted while it was being checked")
    _, current = await _call(toolset, "get_active_artifact_store")
    assert current["provider_id"] == "artifact-storage-default"
