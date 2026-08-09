"""ExternalToolDef / ExternalToolCall model tests."""

import pytest
from pydantic import ValidationError

from primer.model.external_tool import (
    ExternalToolCall,
    ExternalToolDef,
    validate_external_tool_defs,
)


def _def(**over):
    base = dict(
        name="lookup_customer",
        description="Look up a customer in the host CRM.",
        args_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    )
    base.update(over)
    return ExternalToolDef(**base)


def test_def_accepts_valid_shape_and_schema_alias():
    d = _def()
    assert d.name == "lookup_customer"
    assert d.timeout_seconds is None
    # wire alias: constructor accepts "schema", dumps as "schema"
    d2 = ExternalToolDef(
        name="pick_date", description="d", schema={"type": "object"}
    )
    assert d2.args_schema == {"type": "object"}
    assert "schema" in d2.model_dump(by_alias=True)


@pytest.mark.parametrize(
    "bad", ["Foo", "1abc", "has-dash", "has__dunder", "a" * 65, ""]
)
def test_def_rejects_bad_names(bad):
    with pytest.raises(ValidationError):
        _def(name=bad)


def test_def_rejects_malformed_args_schema():
    with pytest.raises(ValidationError):
        _def(args_schema={"type": "not-a-type"})


def test_def_rejects_nonpositive_timeout():
    with pytest.raises(ValidationError):
        _def(timeout_seconds=0)


def test_validate_defs_rejects_duplicates_and_count_cap():
    with pytest.raises(ValueError, match="duplicate"):
        validate_external_tool_defs([_def(), _def()])
    many = [_def(name=f"tool_{i}") for i in range(65)]
    with pytest.raises(ValueError, match="64"):
        validate_external_tool_defs(many)


def test_validate_defs_rejects_size_cap():
    fat = _def(
        args_schema={
            "type": "object",
            "description": "x" * (256 * 1024),
        }
    )
    with pytest.raises(ValueError, match="256"):
        validate_external_tool_defs([fat])


def test_call_row_defaults_and_id_prefix():
    row = ExternalToolCall(
        session_id="sess-1",
        tool_call_id="tc-1",
        tool_name="lookup_customer",
        arguments={"id": "c1"},
    )
    assert row.id.startswith("etool-")
    assert row.status == "pending"
    assert row.chat_id is None and row.node_id is None
    assert row.result is None and row.is_error is False
    assert row.resolved_at is None and row.timeout_at is None
