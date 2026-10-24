"""``system::list_model_profile_choices``: the tool twin of ``GET /v1/model_profiles/choices`` (audit G3-02).

``list_model_profiles`` is admin-only, as its REST router, because a profile is provider configuration. A user-role agent may still write
agents (``create_agent`` / ``update_agent`` are user tier) and an agent must name a profile, so this user-tier tool lists every profile by id,
description, kind and context length, sorted by id and paged, and nothing of the provider, the model name, the members or the config. Pinned
on a real SQLite store through the real system toolset (the ``world`` fixture of ``test_system_crud_guards``).
"""

from __future__ import annotations

import json

import pytest

from primer.mcp.safety import is_exposable
from primer.model.model_profile import ModelProfile, ModelProfileConfig
from primer.model.principal import PrincipalRef
from primer.model.storage import CursorPage, OffsetPage, OrderBy
from primer.model.yield_ import ToolContext
from tests.toolset._desc_conformance import assert_tool_conforms
from tests.toolset.test_system_crud_guards import world  # noqa: F401  (world is a fixture)

_TOOL = "list_model_profile_choices"


def _ctx(role: str) -> ToolContext:
    return ToolContext(
        tool_call_id="call-1", session_id="sess-1", workspace_id=None,
        initiated_by=PrincipalRef(type="user", id="user-1", display="user", role=role, source="local"),
    )


async def _call(toolset, *, ctx: ToolContext | None = None, **args):
    result = await toolset.call(tool_name=_TOOL, arguments=args, ctx=ctx or _ctx("user"))
    try:
        body = json.loads(result.output)
    except ValueError:
        body = result.output
    return result.is_error, body


async def _tool(toolset, name: str):
    async for tool in toolset.list_tools():
        if tool.id == name:
            return tool
    raise AssertionError(f"the toolset has no tool {name!r}")


def _single(profile_id: str, *, context_length: int = 1000) -> ModelProfile:
    # anthropic-1 is the provider the world fixture seeds.
    return ModelProfile(
        id=profile_id, description=f"{profile_id} profile", provider_id="anthropic-1", model_name="private-wire-model",
        context_length=context_length, config=ModelProfileConfig(extended={"x-private-knob": "private-knob-value"}),
    )


async def _every_id(sp) -> list[str]:
    page = await sp.get_storage(ModelProfile).list(OffsetPage(offset=0, length=200), order_by=[OrderBy(field="id", direction="asc")])
    return [row.id for row in page.items]


@pytest.mark.asyncio
async def test_the_tool_is_user_tier_conforms_and_is_reachable_over_mcp(world) -> None:
    _, toolset, _ = world

    tool = await _tool(toolset, _TOOL)

    assert tool.required_role == "user" and toolset.required_role(_TOOL) == "user"
    assert_tool_conforms(tool)
    assert tool.args_schema.get("required", []) == [], "every argument is optional"
    assert is_exposable(tool, provider=toolset) == (True, None)


@pytest.mark.asyncio
async def test_the_full_profile_tools_stay_admin_only(world) -> None:
    _, toolset, _ = world

    for name in ("list_model_profiles", "get_model_profile", "find_model_profiles"):
        assert (await _tool(toolset, name)).required_role == "admin", name


@pytest.mark.asyncio
async def test_a_user_run_lists_the_choices_sorted_by_id_and_nothing_else(world) -> None:
    sp, toolset, _ = world
    for row in (_single("zz-b", context_length=8192), _single("aa-a")):
        await sp.get_storage(ModelProfile).create(row)
    await sp.get_storage(ModelProfile).create(ModelProfile(id="mm-pool", description="a pool", kind="aggregated", members=["aa-a", "zz-b"]))

    is_error, answer = await _call(toolset)

    assert not is_error, answer
    assert [item["id"] for item in answer["items"]] == await _every_id(sp)
    by_id = {item["id"]: item for item in answer["items"]}
    assert by_id["zz-b"] == {"id": "zz-b", "description": "zz-b profile", "kind": "single", "context_length": 8192}
    assert by_id["mm-pool"] == {"id": "mm-pool", "description": "a pool", "kind": "aggregated", "context_length": None}
    raw = json.dumps(answer)
    for private in ("anthropic-1", "private-wire-model", "x-private-knob", "private-knob-value", "members"):
        assert private not in raw, private


@pytest.mark.asyncio
async def test_the_tool_answers_what_the_route_answers(world) -> None:
    """The route and the tool read through the same function: a page from one is the page from the other."""
    from primer.model_profile.choices import list_model_profile_choices

    sp, toolset, _ = world
    for n in (3, 1, 2):
        await sp.get_storage(ModelProfile).create(_single(f"p-{n}"))

    _, answer = await _call(toolset, limit=2, offset=1)
    direct = await list_model_profile_choices(sp.get_storage(ModelProfile), OffsetPage(offset=1, length=2))

    assert answer == direct.model_dump(mode="json")


@pytest.mark.asyncio
async def test_the_tool_pages_by_offset_and_by_cursor(world) -> None:
    sp, toolset, _ = world
    for n in (3, 1, 2, 5, 4):
        await sp.get_storage(ModelProfile).create(_single(f"p-{n}"))
    every = await _every_id(sp)

    _, first = await _call(toolset, limit=2)
    _, rest = await _call(toolset, limit=200, offset=2)
    # A cursor walk starts where a previous cursor page ended: the first one is read here, the next through the tool.
    from primer.model_profile.choices import list_model_profile_choices

    opening = await list_model_profile_choices(sp.get_storage(ModelProfile), CursorPage(cursor=None, length=2))
    _, by_cursor = await _call(toolset, limit=2, cursor=opening.next_cursor)

    assert [i["id"] for i in first["items"]] + [i["id"] for i in rest["items"]] == every
    assert first["total"] == len(every)
    assert [i.id for i in opening.items] == every[:2] and opening.next_cursor
    assert by_cursor["kind"] == "cursor" and [i["id"] for i in by_cursor["items"]] == every[2:4]


@pytest.mark.asyncio
async def test_an_offset_and_a_cursor_together_are_refused(world) -> None:
    _, toolset, _ = world

    is_error, answer = await _call(toolset, offset=1, cursor="x")

    assert is_error and answer["type"] == "validation-error"


@pytest.mark.asyncio
async def test_a_limit_above_the_page_cap_is_refused(world) -> None:
    _, toolset, _ = world

    is_error, answer = await _call(toolset, limit=201)

    assert is_error and answer["type"] == "validation-error"
