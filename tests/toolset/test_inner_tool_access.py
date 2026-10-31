"""The inner door re-checks the person's toolset access (access-control P4 Task 2): ``call_tool``, ``list_toolset_tools``, ``describe_tool`` and the
approved re-run. A per-tool public flag opens exactly that tool, never its siblings.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.model.access import Access
from primer.model.chat import Tool, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.provider import SqliteConfig, Toolset, ToolsetProviderType
from primer.model.yield_ import ToolContext
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset._system_crud import _call_tool_tool, _describe_tool_tool, _list_toolset_tools_tool

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
USER = PrincipalRef(type="user", id="u-user", display="user", role="user", source="local", user_id="u-user")
OWNER = PrincipalRef(type="user", id="u-owner", display="owner", role="user", source="local", user_id="u-owner")


class _Provider:
    """A toolset with tools ``t1`` and ``t2``; it records what ran."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def required_role(self, tool_name: str) -> str:
        return "user"

    def is_yielding(self, tool_name: str) -> bool:
        return False

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        for name in ("t1", "t2"):
            yield Tool(id=name, description=f"the tool {name}", toolset_id="box", args_schema=_SCHEMA)

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:
        self.ran.append(tool_name)
        return ToolCallResult(output=json.dumps({"ran": tool_name}), is_error=False)


@pytest.fixture
async def world(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    await sp.get_storage(Toolset).create(Toolset(
        id="box", provider=ToolsetProviderType.INTERNAL, access=Access(owners=["u-owner"], public_tools=["t1"]),
    ))
    provider = _Provider()

    class _Registry:
        storage_provider = sp

        async def get_toolset(self, tid: str):
            return provider

    try:
        yield sp, _Registry(), provider
    finally:
        await sp.aclose()


def _ctx(who: PrincipalRef) -> ToolContext:
    return ToolContext(tool_call_id="c1", session_id="s1", workspace_id="w1", initiated_by=who)


async def _call(registry, who: PrincipalRef, tool: str) -> tuple[bool, dict]:
    _, (_, handler) = _call_tool_tool(registry)
    result = await handler({"toolset_id": "box", "tool_name": tool, "arguments": {}}, ctx=_ctx(who))
    return result.is_error, json.loads(result.output)


async def test_call_tool_reaches_a_public_tool_of_a_private_toolset(world) -> None:
    _, registry, provider = world
    is_error, body = await _call(registry, USER, "t1")
    assert not is_error, body
    assert provider.ran == ["t1"]


async def test_call_tool_refuses_its_sibling(world) -> None:
    _, registry, provider = world
    is_error, body = await _call(registry, USER, "t2")
    assert is_error and body["type"] == "not-found", body
    assert "its tools: t1" in body["message"], "only the usable tool is offered by name"
    assert provider.ran == []


async def test_the_owner_reaches_every_tool(world) -> None:
    _, registry, provider = world
    assert not (await _call(registry, OWNER, "t2"))[0]
    assert provider.ran == ["t2"]


async def test_list_toolset_tools_lists_only_the_usable_tools(world) -> None:
    _, registry, _ = world
    _, (_, handler) = _list_toolset_tools_tool(registry)
    body = json.loads((await handler({"toolset_id": "box"}, ctx=_ctx(USER))).output)
    assert [t["id"] for t in body["tools"]] == ["t1"]
    body = json.loads((await handler({"toolset_id": "box"}, ctx=_ctx(OWNER))).output)
    assert [t["id"] for t in body["tools"]] == ["t1", "t2"]


async def test_describe_tool_answers_any_other_tool_as_missing(world) -> None:
    _, registry, _ = world
    _, (_, handler) = _describe_tool_tool(registry)
    ok = await handler({"toolset_id": "box", "tool_name": "t1"}, ctx=_ctx(USER))
    assert not ok.is_error, ok.output
    missing = await handler({"toolset_id": "box", "tool_name": "t2"}, ctx=_ctx(USER))
    assert missing.is_error and json.loads(missing.output)["type"] == "not-found"


async def test_approved_rerun_after_revoke_is_refused(world) -> None:
    """The approved re-run goes through ``dispatch_inner_tool``: a tool whose public flag was revoked since the first call is refused."""
    sp, _, provider = world
    ctx = _ctx(USER)

    async def _rerun(tool: str) -> ToolCallResult:
        return await dispatch_inner_tool(
            provider=provider, toolset_id="box", tool_name=tool, arguments={}, ctx=ctx, storage_provider=sp,
        )

    assert not (await _rerun("t1")).is_error
    row = await sp.get_storage(Toolset).get("box")
    await sp.get_storage(Toolset).update(row.model_copy(update={"access": Access(owners=["u-owner"])}))

    refused = await _rerun("t1")

    assert refused.is_error and json.loads(refused.output)["type"] == "not-found"
    assert provider.ran == ["t1"], "the revoked tool did not run again"


async def test_no_storage_provider_skips_the_check() -> None:
    provider = _Provider()
    result = await dispatch_inner_tool(provider=provider, toolset_id="box", tool_name="t2", arguments={}, ctx=_ctx(USER))
    assert not result.is_error
    assert SimpleNamespace  # keep the import used
