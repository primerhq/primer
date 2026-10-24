"""The collection tool doors refuse the ids the platform reserves (ticket 01a16cad), as ``POST /v1/collections`` does.

The tool half of ``tests/api/test_collection_reserved_ids.py``. ``system::create_collection`` runs the same ``check_collection_id`` as the
REST route, so an id that starts with ``_internal_`` or is the platform wiki's (``system``) is answered ``type=validation-error`` naming
``id`` and nothing is stored. The other doors reach that same handler: ``system__call_tool`` (directly and through the tool manager, the
path an agent takes) and ``tools/call`` over the MCP server. The builder's ``crud`` toolset has no collection tools at all, which a test
here pins so a later one cannot appear without the check.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from primer.agent.tool_manager import ToolExecutionManager
from primer.api.registries import ProviderRegistry
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import build_mcp_server, current_actor
from primer.model.chat import ToolCallPart
from primer.model.collection import Collection
from primer.model.principal import Principal, PrincipalRef
from primer.model.provider import SqliteConfig
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.crud import build_crud_toolset
from primer.toolset.system import build_system_toolset
from tests.agent.test_inner_dispatch import ADMIN, USER, _Session

# Spelled out on purpose, as in the REST test.
RESERVED_IDS = ["_internal_agents", "_internal_tools", "_internal_whatever", "system"]


@pytest.fixture
async def door(tmp_path: Path):
    """A real SQLite store, the real system toolset, and the registry ``call_tool`` and the MCP dispatch resolve it through."""
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    registry = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    toolset = build_system_toolset(storage_provider=sp, provider_registry=registry)
    registry._system_toolset_provider = toolset  # noqa: SLF001
    yield sp, toolset, ExposureDeps(storage_provider=sp, provider_registry=registry)
    await sp.aclose()


def _ctx(principal: PrincipalRef) -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id="ws-1", initiated_by=principal)


def _assert_reserved(output: str, cid: str) -> None:
    body = json.loads(output)
    assert body["type"] == "validation-error", body
    assert body["message"].startswith("id: "), body
    assert repr(cid) in body["message"] and "reserved for the platform" in body["message"], body


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", RESERVED_IDS)
async def test_create_collection_refuses_a_reserved_id_and_stores_nothing(door, cid: str) -> None:
    sp, toolset, _ = door

    result = await toolset.call(tool_name="create_collection", arguments={"entity": {"id": cid, "description": "mine"}})

    assert result.is_error, result.output
    _assert_reserved(result.output, cid)
    assert await sp.get_storage(Collection).get(cid) is None


@pytest.mark.asyncio
async def test_create_collection_still_creates_an_ordinary_id(door) -> None:
    sp, toolset, _ = door

    result = await toolset.call(tool_name="create_collection", arguments={"entity": {"id": "internal_agents", "description": "mine"}})

    assert not result.is_error, result.output
    assert (await sp.get_storage(Collection).get("internal_agents")).system is False


@pytest.mark.asyncio
async def test_the_create_descriptor_tells_an_agent_about_the_reserved_ids(door) -> None:
    """An agent reads the descriptor before it writes."""
    _, toolset, _ = door

    described = {tool.id: tool.description async for tool in toolset.list_tools()}

    assert "_internal_" in described["create_collection"] and "reserved for the platform" in described["create_collection"]


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [USER, ADMIN], ids=["user", "admin"])
@pytest.mark.parametrize("cid", RESERVED_IDS)
async def test_call_tool_reaches_the_same_refusal(door, principal: PrincipalRef, cid: str) -> None:
    sp, toolset, _ = door

    result = await toolset.call(tool_name="call_tool", ctx=_ctx(principal), arguments={
        "toolset_id": "system", "tool_name": "create_collection", "arguments": {"entity": {"id": cid, "description": "mine"}}})

    assert result.is_error, result.output
    _assert_reserved(result.output, cid)
    assert await sp.get_storage(Collection).get(cid) is None


@pytest.mark.asyncio
async def test_system_call_tool_through_the_tool_manager_is_refused_too(door) -> None:
    """The path an agent takes: ``system__call_tool`` dispatched by the tool manager for a user-role run."""
    sp, toolset, _ = door
    mgr = ToolExecutionManager.for_workspace(
        toolset_providers={"system": toolset}, session=_Session("s-1", "ws-1"), tools=["system__call_tool"], initiated_by=USER,
    )

    part = await mgr.execute(ToolCallPart(id="c1", name="system__call_tool", arguments={
        "toolset_id": "system", "tool_name": "create_collection", "arguments": {"entity": {"id": "_internal_agents", "description": "x"}}}))

    assert part.error is True, part.output
    _assert_reserved(part.output, "_internal_agents")
    assert await sp.get_storage(Collection).get("_internal_agents") is None


@asynccontextmanager
async def _mcp_session(deps: ExposureDeps) -> AsyncIterator[ClientSession]:
    async with InMemoryTransport(build_mcp_server(lambda: deps)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", ["_internal_agents", "system"])
async def test_mcp_tools_call_is_refused_and_an_ordinary_id_still_creates(door, cid: str) -> None:
    """``tools/call`` over the MCP server, as an admin, with ``system__create_collection`` exposed."""
    sp, _, deps = door
    await update_exposure(enabled=True, allowed_tools=["system__create_collection"], updated_by="admin", deps=deps)
    token = current_actor.set(Principal(type="user", id="admin-1", display="admin", role="admin", source="local"))
    try:
        async with _mcp_session(deps) as client:
            refused = await client.call_tool("system__create_collection", arguments={"entity": {"id": cid, "description": "x"}})
            created = await client.call_tool("system__create_collection", arguments={"entity": {"id": "mine", "description": "x"}})
    finally:
        current_actor.reset(token)

    assert refused.is_error is True
    _assert_reserved(refused.content[0].text, cid)
    assert await sp.get_storage(Collection).get(cid) is None
    assert created.is_error is False, created.content
    assert await sp.get_storage(Collection).get("mine") is not None


@pytest.mark.asyncio
async def test_the_crud_toolset_has_no_collection_door(door) -> None:
    """The builder's ``crud`` toolset re-homes the agent, graph, trigger and python-toolset writes only. A collection tool added there
    must run ``check_collection_id`` (as ``primer/toolset/system.py`` does) and be added to the tests above."""
    sp, _, _ = door

    crud = build_crud_toolset(storage_provider=sp)

    assert [tool.id async for tool in crud.list_tools() if "collection" in tool.id] == []
