"""The system ``create_toolset`` / ``update_toolset`` tools refuse an MCP ``stdio`` toolset unless the CALLER is an admin
(architecture review A-02, the tool half of ``tests/api/test_toolset_stdio_admin.py``).

The tools are declared ``required_role="user"`` (so an agent can author http / python toolsets), and the tool manager's floor only
compares that static role. A stdio toolset launches a command on the server host, so the handler checks the caller itself: the
run's ``initiated_by`` (``ToolContext``) must satisfy ``admin`` under the same predicate the floor uses (``primer.authz._role_allows``:
an admin, or the ``system`` internal actor; a ``trigger``-typed run is ranked by its role, security review A-20). A call that carries no identity (the MCP endpoint hands handlers none)
fails closed for stdio and is unchanged for everything else.
"""

from __future__ import annotations

import json

import pytest

from primer.api.registries import ProviderRegistry
from primer.model.provider import Toolset
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset
from tests._support.caller import caller as _ctx
from tests.conftest import _FakeStorageProvider

STDIO = {"id": "ts-stdio", "provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["/bin/echo", "hi"]}}}
STDIO_OTHER = {**STDIO, "config": {"transport": "stdio", "config": {"command": ["/bin/sh", "-c", "id"]}}}
HTTP = {"id": "ts-http", "provider": "mcp", "config": {"transport": "http", "config": {"url": "http://127.0.0.1:9/mcp"}}}


@pytest.fixture
def toolset_and_storage():
    sp = _FakeStorageProvider()
    reg = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda p: object(),
    )
    return build_system_toolset(storage_provider=sp, provider_registry=reg), sp.get_storage(Toolset)


async def _call(toolset, tool: str, arguments: dict, ctx: ToolContext | None):
    return await toolset.call(tool_name=tool, arguments=arguments, principal=None, ctx=ctx)


def _error_type(result) -> str | None:
    return json.loads(result.output).get("type") if result.is_error else None


async def test_a_user_run_cannot_create_a_stdio_toolset(toolset_and_storage):
    toolset, storage = toolset_and_storage

    result = await _call(toolset, "create_toolset", {"entity": STDIO}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert await storage.get("ts-stdio") is None, "the refused toolset was stored"


async def test_a_call_that_carries_no_identity_cannot_create_a_stdio_toolset(toolset_and_storage):
    """The MCP endpoint dispatches without a ToolContext: the caller's role is unknown there, so a stdio toolset fails closed."""
    toolset, storage = toolset_and_storage

    result = await _call(toolset, "create_toolset", {"entity": STDIO}, None)

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert await storage.get("ts-stdio") is None


async def test_a_user_run_cannot_change_an_existing_stdio_toolset(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": STDIO}, _ctx("admin"))).is_error

    result = await _call(toolset, "update_toolset", {"id": "ts-stdio", "entity": STDIO_OTHER}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert (await storage.get("ts-stdio")).config.config.command == ["/bin/echo", "hi"]


async def test_a_user_run_cannot_turn_an_http_toolset_into_a_stdio_one(toolset_and_storage):
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": HTTP}, _ctx("user"))).is_error

    result = await _call(toolset, "update_toolset", {"id": "ts-http", "entity": {**STDIO, "id": "ts-http"}}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert (await storage.get("ts-http")).config.transport.value == "http"


async def test_a_user_run_cannot_repoint_a_stdio_toolset_at_http(toolset_and_storage):
    """The stored row counts too: the incoming body launches nothing, so only the ``existing`` side of the rule can refuse it."""
    toolset, storage = toolset_and_storage
    assert not (await _call(toolset, "create_toolset", {"entity": STDIO}, _ctx("admin"))).is_error

    result = await _call(toolset, "update_toolset", {"id": "ts-stdio", "entity": {**HTTP, "id": "ts-stdio"}}, _ctx("user"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert (await storage.get("ts-stdio")).config.transport.value == "stdio"


@pytest.mark.parametrize("ctx", [_ctx("admin"), _ctx(None, kind="system")], ids=["admin", "system"])
async def test_an_admin_or_an_internal_actor_can_create_and_change_a_stdio_toolset(toolset_and_storage, ctx):
    toolset, storage = toolset_and_storage

    created = await _call(toolset, "create_toolset", {"entity": STDIO}, ctx)
    changed = await _call(toolset, "update_toolset", {"id": "ts-stdio", "entity": STDIO_OTHER}, ctx)

    assert not created.is_error and not changed.is_error, (created.output, changed.output)
    assert (await storage.get("ts-stdio")).config.config.command == ["/bin/sh", "-c", "id"]


async def test_a_trigger_typed_run_without_an_admin_role_cannot_create_a_stdio_toolset(toolset_and_storage):
    """A trigger-typed identity (a legacy ownerless trigger's run) is no longer an internal actor (security review A-20)."""
    toolset, storage = toolset_and_storage

    result = await _call(toolset, "create_toolset", {"entity": STDIO}, _ctx("user", kind="trigger"))

    assert result.is_error and _error_type(result) == "forbidden", result.output
    assert await storage.get("ts-stdio") is None


@pytest.mark.parametrize("ctx", [_ctx("user"), None], ids=["user", "no-identity"])
async def test_toolsets_that_launch_nothing_stay_available_to_the_tool_surface(toolset_and_storage, ctx):
    toolset, storage = toolset_and_storage

    result = await _call(toolset, "create_toolset", {"entity": HTTP}, ctx)

    assert not result.is_error, result.output
    assert await storage.get("ts-http") is not None
