"""Signature to JSON Schema. ctx is injected, never a schema property."""

from __future__ import annotations

import ast

import pytest

from primer.toolset.python_runner.docstring import parse_docstring
from primer.toolset.python_runner.schema import SchemaError, build_args_schema

DOC = "Do a thing.\n\nUse when you must.\n\nArgs:\n    a: The a.\n    b: The b.\n"


def _schema(src: str, doc: str):
    node = ast.parse(src).body[0]
    return build_args_schema(node, parse_docstring(doc))


def test_annotated_params_become_properties() -> None:
    s = _schema("def f(a: str, b: int) -> str: ...", DOC)
    assert s["properties"]["a"]["type"] == "string"
    assert s["properties"]["b"]["type"] == "integer"
    assert set(s["required"]) == {"a", "b"}


def test_arg_descriptions_come_from_the_docstring() -> None:
    s = _schema("def f(a: str, b: int) -> str: ...", DOC)
    assert s["properties"]["a"]["description"] == "The a."


def test_a_default_makes_the_param_optional() -> None:
    s = _schema("def f(a: str, b: int = 3) -> str: ...", DOC)
    assert s["required"] == ["a"]
    assert s["properties"]["b"]["default"] == 3


def test_ctx_is_excluded_from_the_schema() -> None:
    doc = "Do a thing.\n\nUse when you must.\n\nArgs:\n    a: The a.\n"
    s = _schema("def f(a: str, ctx: ToolContext) -> str: ...", doc)
    assert "ctx" not in s["properties"]
    assert s["required"] == ["a"]


def test_an_unannotated_param_is_an_error_naming_it() -> None:
    doc = "Do a thing.\n\nUse when you must.\n\nArgs:\n    a: The a.\n"
    with pytest.raises(SchemaError) as exc:
        _schema("def f(a) -> str: ...", doc)
    assert exc.value.field == "a"


def test_an_undocumented_param_is_an_error_naming_it() -> None:
    doc = "Do a thing.\n\nUse when you must.\n\nArgs:\n    a: The a.\n"
    with pytest.raises(SchemaError) as exc:
        _schema("def f(a: str, b: int) -> str: ...", doc)
    assert exc.value.field == "b"


def test_varargs_and_kwargs_are_rejected() -> None:
    doc = "Do a thing.\n\nUse when you must.\n\nArgs:\n    a: The a.\n"
    with pytest.raises(SchemaError):
        _schema("def f(a: str, *rest) -> str: ...", doc)
    with pytest.raises(SchemaError):
        _schema("def f(a: str, **kw) -> str: ...", doc)


def test_the_schema_is_self_contained() -> None:
    # make_tool validates examples against it with Draft202012Validator, which
    # cannot resolve external refs.
    s = _schema("def f(a: str, b: int) -> str: ...", DOC)
    assert "$defs" not in s
    assert s["type"] == "object"
    assert s["additionalProperties"] is False


def test_optional_and_list_types_survive() -> None:
    doc = "Do a thing.\n\nUse when you must.\n\nArgs:\n    a: The a.\n"
    s = _schema("def f(a: list[str] | None = None) -> str: ...", doc)
    assert s["properties"]["a"]["type"] == "array"
    assert s["properties"]["a"]["items"]["type"] == "string"


def test_an_unmappable_annotation_is_an_error_naming_the_param() -> None:
    doc = "Do a thing.\n\nUse when you must.\n\nArgs:\n    a: The a.\n"
    with pytest.raises(SchemaError) as exc:
        _schema("def f(a: SomeCustomClass) -> str: ...", doc)
    assert exc.value.field == "a"


def test_an_async_function_is_handled_the_same() -> None:
    s = _schema("async def f(a: str, b: int) -> str: ...", DOC)
    assert set(s["required"]) == {"a", "b"}
