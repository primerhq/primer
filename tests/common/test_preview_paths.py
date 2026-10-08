"""The path language of the Inbox approval preview allowlist (design note 01a11cd3-66b0, slice 1).

A tool, or the operator's approval policy, names the arguments an approver's card may show as DOTTED PATHS into the argument object: ``path``, ``entity.id``,
``entity.model.profile_id``. A path allows its whole subtree, and a list is transparent (``entity.nodes.agent_id`` is the ``agent_id`` of every node). Everything
else is withheld. ``primer/common/preview_paths.py`` is the leaf module that knows the language, three questions about it:

* is a path well formed (``path_syntax_error``)?
* does the tool's JSON Schema have it (``schema_has_path`` / ``missing_paths``): a path that names nothing is a typo, and a typo hides more than was meant;
* which top-level arguments are SAFE WITHOUT A DECLARATION (``closed_set_names``, the default of design ruling D2): a boolean, an integer, a number, ``null``,
  an enum or a const cannot carry a free-form secret; a string, an object, an array, a schema with no type, and a ``$ref`` that cannot be resolved are hidden;
* at run time, is the value at ``here`` shown whole, shown in part (some allowed path goes deeper) or hidden (``classify``)?
"""

from __future__ import annotations

import pytest

from primer.common.preview_paths import classify, closed_set_names, missing_paths, path_syntax_error, schema_has_path

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "mode": {"enum": ["read", "write"], "type": "string"},
        "kind": {"const": "file"},
        "recursive": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1},
        "ratio": {"type": "number"},
        "nothing": {"type": "null"},
        "maybe": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
        "either": {"anyOf": [{"type": "boolean"}, {"type": "string"}]},
        "numbers": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "flags": {"type": "array", "items": {"type": "boolean"}},
        "entity": {"$ref": "#/$defs/Entity"},
        "free": {"type": "object"},
        "untyped": {},
        "dangling": {"$ref": "#/$defs/Nope"},
        "closed_ref": {"$ref": "#/$defs/Level"},
    },
    "$defs": {
        "Entity": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "model": {"$ref": "#/$defs/Model"},
                "tools": {"type": "array", "items": {"$ref": "#/$defs/Tool"}},
                "config": {"anyOf": [{"$ref": "#/$defs/Cron"}, {"$ref": "#/$defs/Hook"}]},
                "self": {"$ref": "#/$defs/Entity"},
            },
        },
        "Model": {"type": "object", "properties": {"profile_id": {"type": "string"}}},
        "Tool": {"type": "object", "properties": {"name": {"type": "string"}}},
        "Cron": {"type": "object", "properties": {"kind": {"const": "cron"}, "cron": {"type": "string"}}},
        "Hook": {"type": "object", "properties": {"kind": {"const": "hook"}, "token": {"type": "string"}}},
        "Level": {"enum": ["low", "high"]},
    },
}


# ---- the default of ruling D2: only a closed set is safe without a declaration ----------------------------------------------------------------------------------


def test_only_arguments_whose_schema_is_a_closed_set_are_shown_by_default() -> None:
    assert closed_set_names(SCHEMA) == ["mode", "kind", "recursive", "limit", "ratio", "nothing", "maybe", "numbers", "closed_ref"]


@pytest.mark.parametrize("name", ["path", "either", "tags", "flags", "entity", "free", "untyped", "dangling"])
def test_a_string_an_object_an_array_an_open_union_and_an_unprovable_schema_are_hidden(name: str) -> None:
    """``flags`` is an array of booleans and is still hidden: only the top-level scalar types are the closed set. ``dangling`` points at a ``$ref`` that does not
    resolve and ``untyped`` has no type: a schema that cannot be proven closed counts as hidden."""
    assert name not in closed_set_names(SCHEMA)


def test_a_schema_with_no_properties_has_no_safe_names() -> None:
    assert closed_set_names({"type": "object"}) == []
    assert closed_set_names({}) == []


def test_a_self_referencing_ref_does_not_hang_the_closed_set() -> None:
    loop = {"properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"$ref": "#/$defs/A"}}}

    assert closed_set_names(loop) == []


# ---- a path must name something the tool has --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["path", "entity", "entity.id", "entity.model", "entity.model.profile_id", "entity.tools.name", "entity.config.cron", "entity.config.token",
     "free", "free.anything", "free.anything.deeper", "entity.self.self.id"],
)
def test_a_path_the_schema_has_is_accepted(path: str) -> None:
    assert schema_has_path(SCHEMA, path)


@pytest.mark.parametrize(
    "path", ["nope", "entity.nope", "entity.model.nope", "path.deeper", "dangling.x", "entity.tools.nope", "entity.id.deeper", "Entity", "entity.Model"],
)
def test_a_path_the_schema_lacks_is_refused(path: str) -> None:
    assert not schema_has_path(SCHEMA, path)


def test_missing_paths_lists_each_refused_path_once_in_order() -> None:
    assert missing_paths(SCHEMA, ["path", "nope", "entity.id", "entity.nope", "nope"]) == ["nope", "entity.nope"]
    assert missing_paths(SCHEMA, []) == []


# ---- a path is well formed --------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["a", "a.b", "entity.model.profile_id", "a_b.c-d", "A1.b2"])
def test_a_well_formed_path_has_no_syntax_error(path: str) -> None:
    assert path_syntax_error(path) is None


@pytest.mark.parametrize(
    "path", ["", ".", "a.", ".a", "a..b", "a b", "a.b ", "a[]", "a.b[0]", "a\nb", "a" * 201, "a.\x00"],
)
def test_a_malformed_path_has_a_syntax_error_that_says_which_path(path: str) -> None:
    message = path_syntax_error(path)

    assert message and "preview_args" in message


def test_a_path_that_is_not_text_has_a_syntax_error() -> None:
    assert path_syntax_error(None) and path_syntax_error(3) and path_syntax_error(["a"])  # type: ignore[arg-type]


# ---- run time: is the value at this position shown, shown in part, or hidden ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("allowed", "here", "expected"),
    [
        (["mode", "entity.id"], "mode", "all"),
        (["mode", "entity.id"], "entity", "part"),
        (["mode", "entity.id"], "entity.id", "all"),
        (["mode", "entity.id"], "entity.id.x", "all"),
        (["mode", "entity.id"], "entity.model", "none"),
        (["mode", "entity.id"], "other", "none"),
        (["entity"], "entity.model.profile_id", "all"),
        (["entity.model.profile_id"], "entity", "part"),
        (["entity.model.profile_id"], "entity.model", "part"),
        (["entity.model.profile_id"], "entity.model.other", "none"),
        ([], "anything", "none"),
        (["mode"], "mode2", "none"),
        (["mode"], "model", "none"),
    ],
)
def test_classify_says_whole_part_or_none(allowed: list[str], here: str, expected: str) -> None:
    assert classify(allowed, here) == expected
