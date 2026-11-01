"""Characterization: what primer's MCP server exposes and answers, for a catalogue of reserved and user toolsets.

Pins the exposed list, the input pre-check, the refusals (with their reason) and the admin listing, so W10
(walk only the reserved toolsets on tools/list and tools/call) can be shown to change none of them.
"""

from __future__ import annotations

import pytest

from primer.mcp.dispatch import NotExposed, invoke_exposed, list_exposed_tools
from primer.mcp.exposure import ExposureDeps, _clear_routing_cache, get_exposure, list_available_tools
from primer.mcp.safety import tool_scoped_id
from primer.mcp.server import _input_validation_error
from primer.model.chat import Tool
from primer.model.mcp_exposure import McpExposure
from primer.model.provider import Toolset, ToolsetProviderType
from tests.conftest import _FakeStorageProvider
from tests.mcp.conftest import FakeProviderRegistry, FakeToolsetProvider, _make_tool

_STRICT = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
ALLOWED = [
    "misc__uuid_v4", "misc__now", "misc__yield", "workspaces__read_file",
    "ext__hello", "acme__ts__hi", "gone__tool", "misc__missing",
]


def _ws_tool() -> Tool:
    t = _make_tool("workspaces", "read_file")
    return t.model_copy(update={"requires_workspace": True})


async def _deps() -> ExposureDeps:
    _clear_routing_cache()
    sp = _FakeStorageProvider()
    for tid in ("ext", "acme__ts"):
        await sp.get_storage(Toolset).create(Toolset(id=tid, provider=ToolsetProviderType.INTERNAL))
    registry = FakeProviderRegistry({
        "misc": FakeToolsetProvider(
            "misc",
            [_make_tool("misc", "uuid_v4", args_schema=_STRICT), _make_tool("misc", "now"), _make_tool("misc", "yield")],
            yielding={"yield"}, roles={"now": "user", "uuid_v4": "user"},
        ),
        "workspaces": FakeToolsetProvider("workspaces", [_ws_tool()]),
        "ext": FakeToolsetProvider("ext", [_make_tool("ext", "hello")]),
        "acme__ts": FakeToolsetProvider("acme__ts", [_make_tool("acme__ts", "hi")]),
    })
    deps = ExposureDeps(storage_provider=sp, provider_registry=registry)
    row = await get_exposure(deps)
    await sp.get_storage(McpExposure).update(row.model_copy(update={"enabled": True, "allowed_tools": ALLOWED}))
    return deps


async def test_exposed_list() -> None:
    deps = await _deps()
    assert [tool_scoped_id(t) for t, _ in await list_exposed_tools(deps)] == ["misc__uuid_v4", "misc__now"]


async def test_input_precheck() -> None:
    deps = await _deps()
    assert await _input_validation_error(deps, "misc__uuid_v4", {}) == "'n' is a required property"
    assert await _input_validation_error(deps, "misc__uuid_v4", {"n": 1}) is None
    for name in ("ext__hello", "acme__ts__hi", "misc__yield", "nope__x"):
        assert await _input_validation_error(deps, name, {}) is None


async def test_invoke_answers(system_actor) -> None:
    deps = await _deps()
    ok = await invoke_exposed(scoped_id="misc__now", arguments={}, principal="p", deps=deps, actor=system_actor)
    assert (ok.output, ok.is_error) == ("now:{}", False)
    reasons = {}
    for name in ALLOWED[2:] + ["misc__not_allowed", "nodunder"]:
        with pytest.raises(NotExposed) as ei:
            await invoke_exposed(scoped_id=name, arguments={}, principal="p", deps=deps, actor=system_actor)
        reasons[name] = ei.value.reason
    assert reasons == {
        "misc__yield": "yielding_unsupported",
        "workspaces__read_file": "needs_workspace",
        "ext__hello": "not_system_toolset",
        # A user toolset whose id holds "__" is refused too; its reason is not pinned (see Task 78's report).
        "acme__ts__hi": reasons["acme__ts__hi"],
        "gone__tool": "provider_missing",
        "misc__missing": "tool_missing",
        "misc__not_allowed": "not_in_allowlist",
        "nodunder": "not_in_allowlist",
    }


async def test_admin_listing_still_walks_user_toolsets() -> None:
    deps = await _deps()
    rows = {r["scoped_id"]: (r["exposable"], r["reason"], r["currently_allowed"]) for r in await list_available_tools(deps)}
    assert rows == {
        "misc__uuid_v4": (True, None, True),
        "misc__now": (True, None, True),
        "misc__yield": (False, "yielding_unsupported", True),
        "workspaces__read_file": (False, "needs_workspace", True),
        "ext__hello": (False, "not_system_toolset", True),
        "acme__ts__hi": (False, "not_system_toolset", True),
    }
