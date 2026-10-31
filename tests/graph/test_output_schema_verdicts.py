"""A graph node's output_schema verdicts and messages, pinned before the validation was bounded whole (Part 3 Task 109).

Characterization: each fixture schema and instance answers exactly the violation text (or ``None``) it did before GS-17.
"""

from __future__ import annotations

import pytest

from primer.graph._node_refs import _schema_violation

_TREE = {"type": "object", "properties": {"name": {"type": "string"}, "child": {"$ref": "#"}}, "required": ["name"]}

CASES = {
    "valid": ({"type": "object", "properties": {"a": {"type": "integer"}}}, {"a": 1}, None),
    "type": ({"type": "string"}, 1, "1 is not of type 'string'"),
    "required": ({"type": "object", "required": ["a"]}, {}, "'a' is a required property"),
    "pattern": ({"type": "string", "pattern": "^[a-z]+$"}, "ABC", "'ABC' does not match '^[a-z]+$'"),
    "enum": ({"enum": ["x", "y"]}, "z", "'z' is not one of ['x', 'y']"),
    "unique-dupes": ({"type": "array", "uniqueItems": True}, [{"a": 1}, {"a": 1}],
                     "[{'a': 1}, {'a': 1}] has non-unique elements"),
    "unique-distinct": ({"type": "array", "uniqueItems": True}, [{"a": i} for i in range(50)], None),
    "nested": ({"type": "object", "properties": {"o": {"type": "object", "properties": {"n": {"type": "number"}}}}},
               {"o": {"n": "x"}}, "'x' is not of type 'number'"),
    "additional": ({"type": "object", "properties": {"a": {}}, "additionalProperties": False}, {"a": 1, "x": 2},
                   "Additional properties are not allowed ('x' was unexpected)"),
    "any-of": ({"anyOf": [{"type": "string"}, {"type": "object"}]}, 1, "1 is not valid under any of the given schemas"),
    "recursive-valid": (_TREE, {"name": "a", "child": {"name": "b", "child": {"name": "c"}}}, None),
    "recursive-invalid": (_TREE, {"name": "a", "child": {"name": "b", "child": {}}}, "'name' is a required property"),
    "pattern-over-caps": ({"type": "string", "pattern": "x" * 1001}, "x",
                          "output_schema has a pattern that cannot be used: output_schema pattern is 1001 characters long; "
                          "the limit is 1000"),
    "pattern-budget": ({"type": "string", "pattern": r"(a|aa)+$"}, "a" * 60 + "!",
                       "output_schema validation stopped: its patterns used their 1 s matching budget "
                       "(a pattern with overlapping or nested repeats, such as (a|aa)+, backtracks on long values)"),
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_the_verdict_is_unchanged(case: str) -> None:
    schema, instance, expected = CASES[case]
    assert await _schema_violation(instance, schema) == expected
