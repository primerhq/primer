"""Where the Inbox preview allowlist is DECLARED (design note 01a11cd3-66b0, rulings D1 and D2, slice 1).

Two layers carry the declaration: the TOOL (``make_tool(preview_args=...)`` -> ``Tool.preview_args``, in memory only, excluded from serialization like ``yields`` and
``interruptible``) and the operator's POLICY (``ToolApprovalPolicy.preview_args``, stored with the row). Both are dotted paths into the tool's argument object
(``primer/common/preview_paths.py``). ``None`` means "nothing declared here"; an empty list means "show no value at all" (a declaration like any other).

A typo in a tool's own declaration is an import-time error, like a wrong ``make_tool`` example: a path that names nothing hides more than its author meant, and it
would be silent. A typo in a policy is a 422 at write time (``tests/agent/test_approval_checks_preview_args.py``); this file pins the model's own shape rules.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from primer.model.chat import Tool, ToolExample
from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
from primer.toolset._describe import make_tool

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "entity": {"type": "object", "properties": {"id": {"type": "string"}, "config": {"type": "object"}}},
    },
    "required": ["path"],
}
EXAMPLES = [ToolExample(args={"path": "a"})]


def _tool(**extra) -> Tool:
    return make_tool(id="t", toolset_id="ts", purpose="Do it.", when="Use when testing.", args_schema=SCHEMA, examples=EXAMPLES, **extra)


# ---- the tool's own declaration ---------------------------------------------------------------------------------------------------------------------------------


def test_a_tool_declares_nothing_by_default() -> None:
    assert _tool().preview_args is None


def test_a_tool_declares_the_paths_its_card_may_show() -> None:
    tool = _tool(preview_args=("path", "entity.id"))

    assert tool.preview_args == ("path", "entity.id")


def test_an_empty_declaration_is_a_declaration_show_no_value() -> None:
    assert _tool(preview_args=()).preview_args == ()


def test_a_list_is_accepted_and_stored_as_a_tuple() -> None:
    assert _tool(preview_args=["path"]).preview_args == ("path",)


@pytest.mark.parametrize("path", ["nope", "entity.nope", "path.deeper"])
def test_a_path_the_tools_schema_lacks_is_an_error_when_the_tool_is_built(path: str) -> None:
    with pytest.raises(ValueError, match="preview_args") as raised:
        _tool(preview_args=("path", path))

    assert path in str(raised.value) and "'t'" in str(raised.value), "the error names the tool and the path"


@pytest.mark.parametrize("path", ["", "a..b", "a b", ".x"])
def test_a_malformed_path_is_an_error_when_the_tool_is_built(path: str) -> None:
    with pytest.raises(ValueError, match="preview_args"):
        _tool(preview_args=(path,))


def test_the_declaration_is_in_memory_metadata_and_never_serialized() -> None:
    tool = _tool(preview_args=("path",))

    assert "preview_args" not in tool.model_dump()
    assert "preview_args" not in json.loads(tool.model_dump_json())
    assert "preview_args" not in tool.model_dump(by_alias=True)


def test_the_scoped_copy_the_tool_manager_makes_keeps_the_declaration() -> None:
    scoped = _tool(preview_args=("path", "entity.id")).model_copy(update={"id": "ts__t"})

    assert scoped.preview_args == ("path", "entity.id")


def test_a_wire_tool_has_no_declaration() -> None:
    """An MCP tool or an external one arrives with a name, a description and a schema: nothing declared, so the default rule applies."""
    wire = Tool(id="x", toolset_id="mcp", description="d", schema={"type": "object", "properties": {"q": {"type": "string"}}})

    assert wire.preview_args is None


# ---- the operator's policy --------------------------------------------------------------------------------------------------------------------------------------


def _policy(**fields) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(id="p-1", toolset_id="ts", tool_name="t", approval=RequiredApprovalConfig(), **fields)


def test_a_policy_declares_nothing_by_default() -> None:
    assert _policy().preview_args is None


def test_a_policy_declares_paths_and_they_round_trip_through_the_stored_row() -> None:
    policy = _policy(preview_args=["path", "entity.id"])

    row = policy.model_dump(mode="json")
    assert row["preview_args"] == ["path", "entity.id"]
    assert ToolApprovalPolicy.model_validate(row).preview_args == ["path", "entity.id"]


def test_a_policy_row_stored_before_the_field_existed_reads_as_nothing_declared() -> None:
    row = _policy().model_dump(mode="json")
    row.pop("preview_args", None)

    assert ToolApprovalPolicy.model_validate(row).preview_args is None


def test_an_empty_policy_list_is_kept_it_means_show_no_value() -> None:
    assert _policy(preview_args=[]).preview_args == []


@pytest.mark.parametrize("bad", ["", "a..b", "a b", ".x", "x.", "a[0]", "a" * 201])
def test_a_malformed_policy_path_is_a_validation_error_at_the_field(bad: str) -> None:
    with pytest.raises(ValidationError) as raised:
        _policy(preview_args=["path", bad])

    error = raised.value.errors()[0]
    assert error["loc"][0] == "preview_args" and "preview_args" in error["msg"], error


def test_more_than_the_cap_of_paths_is_a_validation_error() -> None:
    with pytest.raises(ValidationError) as raised:
        _policy(preview_args=[f"a{i}" for i in range(65)])

    assert raised.value.errors()[0]["loc"][0] == "preview_args"


def test_the_field_description_says_what_it_does_for_the_openapi_schema() -> None:
    description = ToolApprovalPolicy.model_json_schema()["properties"]["preview_args"]["description"]

    assert "approval card" in description and "dotted" in description
