"""Tool.tool_class: the NOTIFYING tool class (S3 spec section 3)."""

from __future__ import annotations

from primer.model.chat import NOTIFYING_TOOL_RESULT, Tool
from primer.toolset._describe import make_tool


def _schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def test_tool_class_defaults_to_standard() -> None:
    t = Tool(
        id="noop",
        toolset_id="misc",
        description="d",
        args_schema=_schema(),
    )
    assert t.tool_class == "standard"


def test_tool_class_accepts_the_wire_alias_and_the_field_name() -> None:
    aliased = Tool.model_validate(
        {
            "id": "open_file",
            "toolset_id": "client",
            "description": "d",
            "schema": _schema(),
            "class": "notifying",
        }
    )
    assert aliased.tool_class == "notifying"
    named = Tool(
        id="open_file",
        toolset_id="client",
        description="d",
        args_schema=_schema(),
        tool_class="notifying",
    )
    assert named.tool_class == "notifying"


def test_tool_class_is_in_memory_only() -> None:
    t = Tool(
        id="open_file",
        toolset_id="client",
        description="d",
        args_schema=_schema(),
        tool_class="notifying",
    )
    dumped = t.model_dump()
    assert "class" not in dumped
    assert "tool_class" not in dumped


def test_make_tool_declares_the_class() -> None:
    t = make_tool(
        id="open_file",
        toolset_id="client",
        purpose="Open a file.",
        when="Use when the user should look at a file.",
        args_schema=_schema(),
        examples=[],
        tool_class="notifying",
    )
    assert t.tool_class == "notifying"


def test_make_tool_defaults_to_standard() -> None:
    t = make_tool(
        id="plain",
        toolset_id="misc",
        purpose="Do a thing.",
        when="Use when you need a thing.",
        args_schema=_schema(),
        examples=[],
    )
    assert t.tool_class == "standard"


async def test_inform_user_is_declared_notifying() -> None:
    from primer.toolset.misc import build_misc_toolset

    provider = build_misc_toolset()
    tools = {t.id: t async for t in provider.list_tools(principal=None)}
    assert tools["inform_user"].tool_class == "notifying"
    assert tools["get_datetime"].tool_class == "standard"


def test_notifying_result_is_a_successful_json_body() -> None:
    import json

    assert json.loads(NOTIFYING_TOOL_RESULT) == {"delivered": True}
