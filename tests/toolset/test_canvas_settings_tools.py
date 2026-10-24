"""The canvas settings as system tools (pivot P5, Task 15; Decision 14): admin-only, the read and a partial update."""

from __future__ import annotations

import json

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.mcp.safety import is_exposable
from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
from tests._support.caller import caller
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)

DEFAULTS = CanvasSettings(id=CANVAS_SETTINGS_ID).model_dump(mode="json")


async def test_the_settings_tools_are_admin_and_say_what_they_do(world):
    _, toolset, _ = world
    tools = {tool.id: tool async for tool in toolset.list_tools()}
    get, update = tools["get_canvas_settings"], tools["update_canvas_settings"]
    assert get.description.startswith("Read the canvas settings: media policy, strict mode and limits.")
    assert update.description.startswith("Change the canvas settings; fields you leave out keep their values.")
    for tool in (get, update):
        assert toolset.required_role(tool.id) == "admin"
        assert (tool.yields, tool.requires_workspace) == (False, False)
        assert is_exposable(tool, provider=toolset) == (True, None)
    assert set(update.args_schema["properties"]) == set(CanvasSettings.model_fields) - {"id"}
    assert update.args_schema["properties"]["max_elements"]["anyOf"][0] == {"maximum": 1000, "minimum": 1, "type": "integer"}


async def test_the_read_is_the_defaults_until_saved(world):
    sp, toolset, _ = world
    is_error, answer = await _call(toolset, "get_canvas_settings")
    assert not is_error and answer == DEFAULTS
    assert await sp.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID) is None


async def test_an_update_is_partial(world):
    sp, toolset, _ = world
    is_error, answer = await _call(toolset, "update_canvas_settings", media_policy="allowlist", media_allowlist=["Media.Example.com"])
    assert not is_error, answer
    assert (answer["media_policy"], answer["media_allowlist"]) == ("allowlist", ["media.example.com"])
    is_error, answer = await _call(toolset, "update_canvas_settings", strict_mode=True)
    assert not is_error
    assert answer == {**DEFAULTS, "media_policy": "allowlist", "media_allowlist": ["media.example.com"], "strict_mode": True}
    assert (await _call(toolset, "get_canvas_settings"))[1] == answer
    assert (await sp.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID)).strict_mode is True


async def test_an_invalid_update_changes_nothing(world):
    sp, toolset, _ = world
    await _call(toolset, "update_canvas_settings", max_events=50)
    for bad in ({"max_elements": 0}, {"media_policy": "everything"}, {"media_allowlist": ["not a host"]}, {"colour": "red"},
                {"strict_mode": None}):
        is_error, answer = await _call(toolset, "update_canvas_settings", **bad)
        assert is_error and answer["type"] == "validation-error", bad
        assert "not a host" not in answer["message"] or "media_allowlist" in bad, "the input is not echoed"
    assert (await sp.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID)).model_dump(mode="json") == {**DEFAULTS, "max_events": 50}


async def test_a_user_cannot_call_them(world):
    """Through the dispatch every tool call takes (``system__call_tool`` and the agent loop), which enforces the declared role."""
    sp, toolset, _ = world
    user = caller("user")
    for name, args in (("get_canvas_settings", {}), ("update_canvas_settings", {"strict_mode": True})):
        result = await dispatch_inner_tool(provider=toolset, toolset_id="system", tool_name=name, arguments=args, ctx=user)
        assert result.is_error and json.loads(result.output)["type"] == "forbidden", result.output
    assert await sp.get_storage(CanvasSettings).get(CANVAS_SETTINGS_ID) is None
