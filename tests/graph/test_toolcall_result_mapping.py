"""_map_toolcall_result wraps a ToolResultPart into a NodeOutput.

Spec B §2.3 step 4:
- text = result.output
- parsed populated when output_schema validates the JSON-parsed output
- output_schema failure → error_code='tool_output_invalid'
- non-JSON when schema set → error_code='tool_output_invalid'

An error RESULT (``ToolResultPart.error``) is a failed call, the same as a tool that raised: error_code='tool_execution_failed'
with the tool's own output as the message, decided before the output schema is looked at (01a10b50).
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


def test_an_error_result_is_a_failed_call_carrying_the_tools_output() -> None:
    res = _map_toolcall_result(
        ToolResultPart(id="tc-1", output='{"type": "not-found", "message": "no such file"}', error=True),
        output_schema=None,
    )
    assert res.error_code == "tool_execution_failed"
    assert res.error_message == '{"type": "not-found", "message": "no such file"}'
    assert res.text == '{"type": "not-found", "message": "no such file"}'
    assert res.parsed is None


def test_an_error_result_is_judged_before_the_output_schema() -> None:
    """An error envelope that happens to be JSON is not "valid output", and one that is not JSON is not "invalid output"."""
    schema = {"type": "object", "required": ["q"]}
    as_json = _map_toolcall_result(ToolResultPart(id="tc-1", output='{"x": 1}', error=True), output_schema=schema)
    not_json = _map_toolcall_result(ToolResultPart(id="tc-1", output="boom", error=True), output_schema=schema)
    assert as_json.error_code == not_json.error_code == "tool_execution_failed"


def test_a_result_that_is_not_an_error_is_unchanged() -> None:
    res = _map_toolcall_result(ToolResultPart(id="tc-1", output="fine", error=False), output_schema=None)
    assert (res.text, res.error_code) == ("fine", None)
