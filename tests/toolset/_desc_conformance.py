"""Shared assertion: a Tool obeys the description anatomy and its examples
validate against its own args_schema. Used by per-toolset tests and the
global conformance test.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator

from primer.model.chat import Tool


def assert_tool_conforms(tool: Tool) -> None:
    assert tool.description, f"{tool.id}: empty description"
    assert len(tool.description) > 30, f"{tool.id}: too-thin description"
    assert "Use when" in tool.description, f"{tool.id}: missing 'Use when' clause"
    assert "Example:" in tool.description, f"{tool.id}: missing 'Example:' line"
    assert tool.examples, f"{tool.id}: no structured examples"
    validator = Draft202012Validator(tool.args_schema)
    for i, ex in enumerate(tool.examples):
        try:
            validator.validate(ex.args)
        except Exception as exc:  # noqa: BLE001 - re-raise with tool context
            raise AssertionError(
                f"{tool.id}: example #{i} args do not validate: {exc}"
            ) from exc
