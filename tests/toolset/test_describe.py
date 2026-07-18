import pytest
from jsonschema import ValidationError as SchemaError
from pydantic import BaseModel, Field

from primer.model.chat import Tool, ToolExample
from primer.toolset._describe import make_tool, render_description


class _Args(BaseModel):
    name: str = Field(..., description="A name.")
    count: int = Field(default=1, ge=1, description="How many.")


def test_render_description_composes_examples():
    body = "Do the thing.\n\nUse when you need the thing."
    out = render_description(
        body,
        [ToolExample(args={"name": "x"}, returns="ok", note="basic")],
    )
    assert out.startswith("Do the thing.")
    assert "Use when" in out
    assert 'Example: {"name":"x"} -> ok  (basic)' in out


def test_make_tool_builds_tool_with_anatomy():
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x", "count": 2}, returns="ok")],
    )
    assert isinstance(tool, Tool)
    assert tool.examples and tool.examples[0].args == {"name": "x", "count": 2}
    assert "Use when" in tool.description and "Example:" in tool.description


def test_make_tool_rejects_invalid_example():
    with pytest.raises(SchemaError):
        make_tool(
            id="thing",
            toolset_id="misc",
            purpose="Do the thing.",
            when="Use when you need the thing.",
            args_schema=_Args.model_json_schema(),
            examples=[ToolExample(args={"count": 2})],  # missing required 'name'
        )


def test_examples_excluded_from_serialization():
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x"})],
    )
    dumped = tool.model_dump()
    assert "examples" not in dumped  # not on the wire (no /v1/tools change)
    assert dumped["schema"]  # args_schema still serialized under its alias


def test_make_tool_flags_default_false():
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x"})],
    )
    assert tool.yields is False


def test_make_tool_sets_explicit_flags():
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x"})],
        yields=True,
    )
    assert tool.yields is True


def test_make_tool_flags_excluded_from_serialization():
    # The capability flags are in-memory metadata only; the wire shape
    # (/v1/tools, MCP tools/list) must stay unchanged.
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x"})],
        yields=True,
    )
    dumped = tool.model_dump()
    assert "yields" not in dumped


def test_make_tool_requires_workspace_defaults_false():
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x"})],
    )
    assert tool.requires_workspace is False


def test_make_tool_sets_requires_workspace_flag():
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x"})],
        requires_workspace=True,
    )
    assert tool.requires_workspace is True


def test_requires_workspace_excluded_from_serialization():
    # In-memory metadata only; must not appear on the wire.
    tool = make_tool(
        id="thing",
        toolset_id="misc",
        purpose="Do the thing.",
        when="Use when you need the thing.",
        args_schema=_Args.model_json_schema(),
        examples=[ToolExample(args={"name": "x"})],
        requires_workspace=True,
    )
    assert "requires_workspace" not in tool.model_dump()
