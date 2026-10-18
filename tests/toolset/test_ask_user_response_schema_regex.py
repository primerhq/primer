"""A run whose principal is not an admin may not put a regular expression in ``ask_user``'s ``response_schema`` (pivot P1, security re-review of #764).

``ask_user`` is role user, and its ``response_schema`` is the agent's. ``POST /v1/sessions/{id}/ask_user/respond`` validates the answer against it
with jsonschema, synchronously in the API process's event loop, where ``pattern`` and ``patternProperties`` run as Python ``re``: a catastrophic
pattern (``(a+)+$``) and one crafted answer would stall that loop for every user. So a non-admin run is refused ``type=forbidden`` BEFORE it parks
when either keyword appears anywhere in the schema: under every keyword that applies a subschema, and under any other key too, because a
``$ref`` can point at any location of the document. Admin and system runs are unaffected, and so is every other keyword.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset._system_tools import _ask_user_handler

ADMIN = PrincipalRef(type="user", id="u-admin", display="admin", role="admin", source="local")
USER = PrincipalRef(type="user", id="u-1", display="user", role="user", source="local")

BAD = "(a+)+$"
STR = {"type": "string", "pattern": BAD}

# A regex keyword at the top, under every keyword that applies a subschema, and where only a $ref reaches it.
SCHEMAS: dict[str, dict[str, Any]] = {
    "top-level": STR,
    "patternProperties": {"type": "object", "patternProperties": {BAD: {"type": "string"}}},
    "properties": {"type": "object", "properties": {"name": STR}},
    "items": {"type": "array", "items": STR},
    "prefixItems": {"type": "array", "prefixItems": [{"type": "integer"}, STR]},
    "additionalProperties": {"type": "object", "additionalProperties": STR},
    "$defs": {"$defs": {"s": STR}, "$ref": "#/$defs/s"},
    "definitions": {"definitions": {"s": STR}, "$ref": "#/definitions/s"},
    "allOf": {"allOf": [{"type": "string"}, STR]},
    "anyOf": {"anyOf": [{"type": "integer"}, STR]},
    "oneOf": {"oneOf": [{"type": "integer"}, STR]},
    "not": {"not": STR},
    "propertyNames": {"type": "object", "propertyNames": {"pattern": BAD}},
    "if": {"if": STR, "then": {"type": "string"}},
    "then": {"if": {"type": "string"}, "then": STR},
    "else": {"if": {"type": "integer"}, "else": STR},
    "contains": {"type": "array", "contains": STR},
    "dependentSchemas": {"type": "object", "dependentSchemas": {"a": {"properties": {"b": STR}}}},
    "deep": {"properties": {"a": {"items": {"anyOf": [{"properties": {"b": {"not": {"allOf": [STR]}}}}]}}}},
    "unknown-key-via-ref": {"$ref": "#/x-shape", "x-shape": STR},
    "default-value-via-ref": {"$ref": "#/default", "default": STR},
}
# Schemas a non-admin may still use: no regex keyword anywhere (a property merely NAMED pattern is not one).
CLEAN: dict[str, dict[str, Any] | None] = {
    "none": None,
    "typed-object": {"type": "object", "properties": {"pattern": {"type": "string", "minLength": 1}, "n": {"enum": [1, 2]}}, "required": ["n"]},
    "array": {"type": "array", "items": {"type": "string", "maxLength": 20}, "minItems": 1},
}


def _ctx(who: PrincipalRef | None) -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="sess-1", workspace_id="ws-1", initiated_by=who)


async def _ask(schema: dict[str, Any] | None, who: PrincipalRef | None) -> Any:
    arguments: dict[str, Any] = {"prompt": "Which one?"}
    if schema is not None:
        arguments["response_schema"] = schema
    return await _ask_user_handler(arguments, ctx=_ctx(who))


@pytest.mark.parametrize("who", [USER, None], ids=["user", "no-initiator"])
@pytest.mark.parametrize("name", sorted(SCHEMAS))
async def test_a_non_admin_may_not_put_a_regex_in_the_response_schema(name, who):
    result = await _ask(SCHEMAS[name], who)

    assert not isinstance(result, Yielded), "a refused question must not park: the respond route would run its schema"
    assert result.is_error and json.loads(result.output)["type"] == "forbidden", result


async def test_the_refusal_names_every_place_a_regex_is_used():
    schema = {"properties": {"name": STR, "tags": {"items": {"patternProperties": {"^x": {}}}}}, "x": STR}

    result = await _ask(schema, USER)

    assert json.loads(result.output) == {
        "type": "forbidden",
        "message": (
            "this run's principal is not an admin, so its response_schema may not use pattern or patternProperties "
            "(the server runs those regular expressions on the answer); refused at: "
            "'#/properties/name/pattern', '#/properties/tags/items/patternProperties', '#/x/pattern'"
        ),
    }


@pytest.mark.parametrize("who", [ADMIN, PrincipalRef.system()], ids=["admin", "system"])
@pytest.mark.parametrize("name", sorted(SCHEMAS))
async def test_an_admin_keeps_regex_in_the_response_schema(name, who):
    result = await _ask(SCHEMAS[name], who)

    assert isinstance(result, Yielded), result
    assert result.resume_metadata["response_schema"] == SCHEMAS[name]


@pytest.mark.parametrize("who", [USER, None], ids=["user", "no-initiator"])
@pytest.mark.parametrize("name", sorted(CLEAN))
async def test_a_non_admin_may_use_every_other_keyword(name, who):
    result = await _ask(CLEAN[name], who)

    assert isinstance(result, Yielded), result
    assert result.resume_metadata["response_schema"] == CLEAN[name]
