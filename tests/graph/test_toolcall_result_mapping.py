"""_map_toolcall_result wraps a ToolResultPart into a NodeOutput.

Spec B §2.3 step 4:
- text = result.output
- parsed populated when output_schema validates the JSON-parsed output
- output_schema failure → error_code='tool_output_invalid'
- non-JSON when schema set → error_code='tool_output_invalid'
"""

from __future__ import annotations

from primer.model.chat import ToolResultPart
from primer.graph.base import _map_toolcall_result, _ToolCallOutputResult


def _result(output: str) -> ToolResultPart:
    return ToolResultPart(id="tc-1", output=output)


def test_text_only_no_schema() -> None:
    res = _map_toolcall_result(_result("hello"), output_schema=None)
    assert isinstance(res, _ToolCallOutputResult)
    assert res.text == "hello"
    assert res.parsed is None
    assert res.error_code is None


def test_with_schema_validates() -> None:
    schema = {"type": "object", "required": ["q"], "properties": {"q": {"type": "string"}}}
    res = _map_toolcall_result(_result('{"q": "hi"}'), output_schema=schema)
    assert res.parsed == {"q": "hi"}
    assert res.error_code is None


def test_with_schema_invalid_json_returns_error() -> None:
    res = _map_toolcall_result(_result("not json"), output_schema={"type": "object"})
    assert res.error_code == "tool_output_invalid"


def test_with_schema_validation_failure() -> None:
    schema = {"type": "object", "required": ["q"]}
    res = _map_toolcall_result(_result('{"x": 1}'), output_schema=schema)
    assert res.error_code == "tool_output_invalid"
