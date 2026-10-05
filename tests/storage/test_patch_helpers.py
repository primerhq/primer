"""The pure helpers behind patch_if's canonical storage (primer.storage._patch), without a database."""

from __future__ import annotations

import enum
import typing
from typing import Annotated, Any, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, Field, SecretStr

from primer.storage._patch import (
    PatchSpecError,
    _accepts_none,
    json_equal,
    json_identical,
    normalise_where,
)


def test_json_equal_is_the_where_rule_and_json_identical_also_tells_5_from_5_point_0():
    assert json_equal(5, 5.0) and not json_identical(5, 5.0)
    assert json_equal({"a": [1, 2.0]}, {"a": [1.0, 2]}) and not json_identical({"a": [1, 2.0]}, {"a": [1.0, 2]})
    assert not json_equal(True, 1) and not json_identical(True, 1)
    assert not json_equal("5", 5) and json_identical("5", "5")
    assert json_identical(None, None) and not json_identical(None, 0)
    assert json_identical({"a": 1, "b": [True]}, {"b": [True], "a": 1}), "key order is not a difference"


def test_accepts_none_reads_every_way_a_field_can_be_nullable():
    assert _accepts_none(int | None) and _accepts_none(Any)
    assert _accepts_none(Optional[int]) and _accepts_none(Union[int, str, None])  # noqa: UP007, UP045
    assert _accepts_none(Annotated[int | None, "meta"]) and _accepts_none(None)
    assert not _accepts_none(int) and not _accepts_none(Annotated[str, "meta"]) and not _accepts_none(list[int])
    assert not _accepts_none(dict[str, int]) and not _accepts_none(_Color) and not _accepts_none(Literal["a", "b"])


def test_a_form_that_is_not_positively_non_nullable_counts_as_nullable():
    """Wrong in the "not nullable" direction lets a stale guard match a newer null, so unknown forms are nullable."""
    assert _accepts_none(Literal["a", None]) and _accepts_none(object)
    assert _accepts_none(TypeVar("T")), "a type variable: unknown"
    alias = typing.TypeAliasType("MaybeInt", int | None)
    assert _accepts_none(alias), "a PEP 695 alias: unknown, so nullable"


class _Color(str, enum.Enum):
    RED = "red"
    BLUE = "blue"


class _Doc(BaseModel):
    count: int = 0
    color: _Color = _Color.RED
    flag: bool = False
    label: str = "x"
    required: str
    timeout: int | None = 30
    items: list[int] = Field(default_factory=list)
    secret: SecretStr = SecretStr("hunter2")


def test_a_guard_naming_the_default_of_a_non_nullable_field_also_matches_the_absent_key():
    out = normalise_where(_Doc, {
        "count": [0], "color": ["red"], "flag": [False], "label": ["x"],
    })
    assert out == {"count": [0, None], "color": ["red", None], "flag": [False, None], "label": ["x", None]}


def test_a_guard_that_names_another_value_or_already_allows_none_is_left_alone():
    assert normalise_where(_Doc, {"count": [1], "label": ["x", None]}) == {"count": [1], "label": ["x", None]}


def test_nullable_factory_secret_required_and_unknown_fields_are_never_normalised():
    """A nullable field's None means a stored null (a stale 30 must not apply over it); a factory is never called; a
    secret's default would be masked, not what is stored; a required or unknown field has no default to name."""
    where = {"timeout": [30], "items": [[]], "secret": ["hunter2", "**********"], "required": ["r"], "nope": [0]}
    assert normalise_where(_Doc, where) == {k: list(v) for k, v in where.items()}


class _Cnt(BaseModel):
    id: str = "a"
    count: int = 0
    sub: dict[str, Any] = Field(default_factory=dict)


def test_a_whole_number_float_written_to_an_int_field_is_rewritten_to_the_int_spelling():
    """`where` compares 5 and 5.0 by value, but the stored text differs and text comparisons see it."""
    from primer.storage._patch import canonical_fixup

    entity = _Cnt(count=5)
    assert canonical_fixup(entity, {"count": 5.0}, {"count": 5.0}, {}) == {"count": 5}
    assert canonical_fixup(entity, {"count": 5}, {"count": 5}, {}) == {}, "an already canonical write needs no rewrite"


def test_a_set_paths_leaf_the_validated_model_does_not_carry_is_refused():
    import pytest

    from primer.storage._patch import canonical_fixup

    class _Typed(BaseModel):
        id: str = "a"

        class Sub(BaseModel):
            a: int = 0

        sub: Sub = Sub()

    entity = _Typed()
    with pytest.raises(PatchSpecError, match="not part of"):
        canonical_fixup(entity, {"sub": {"a": 0, "typo": 1}}, {}, {("sub", "typo"): 1})
    assert canonical_fixup(entity, {"sub": {"a": "7"}}, {}, {("sub", "a"): "7"}) == {"sub": {"a": 0}}
