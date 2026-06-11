"""Unit tests for the Postgres predicate -> SQL translator.

These assert on the emitted SQL string + bind params and need no live
Postgres, so they run in the narrowed unit sweep. The bool-EQ case is a
regression guard: a JSONB ``->>`` expression yields text, and asyncpg
strictly rejects a Python ``bool`` / ``int`` bind against a text-typed
``$N``. EQ / NE must therefore cast the left side for non-text fields.
"""
from __future__ import annotations

from pydantic import BaseModel

from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.storage._predicate import _PredicateTranslator


class _Model(BaseModel):
    id: str = ""
    name: str = ""
    enabled: bool = False
    count: int = 0
    ratio: float = 0.0
    tags: list[str] | None = None


def _sql(predicate: Predicate) -> tuple[str, list]:
    return _PredicateTranslator(_Model).translate(predicate)


def test_eq_on_bool_field_casts_left_side():
    # The regression: enabled == True must render a ::boolean cast so the
    # bound Python bool binds against a boolean context, not text.
    sql, params = _sql(
        Predicate(left=FieldRef(name="enabled"), op=Op.EQ, right=Value(value=True))
    )
    assert sql == "((data->>'enabled')::boolean = $1)", sql
    assert params == [True]


def test_ne_on_int_field_casts_left_side():
    sql, params = _sql(
        Predicate(left=FieldRef(name="count"), op=Op.NE, right=Value(value=3))
    )
    assert sql == "((data->>'count')::bigint != $1)", sql
    assert params == [3]


def test_eq_on_float_field_casts_left_side():
    sql, params = _sql(
        Predicate(left=FieldRef(name="ratio"), op=Op.EQ, right=Value(value=1.5))
    )
    assert sql == "((data->>'ratio')::double precision = $1)", sql
    assert params == [1.5]


def test_eq_on_str_field_stays_text():
    # str fields have no cast: the text comparison is correct and a bound
    # str binds fine against the text context.
    sql, params = _sql(
        Predicate(left=FieldRef(name="name"), op=Op.EQ, right=Value(value="x"))
    )
    assert sql == "(data->>'name' = $1)", sql
    assert params == ["x"]


def test_eq_on_id_uses_primary_key_column():
    sql, params = _sql(
        Predicate(left=FieldRef(name="id"), op=Op.EQ, right=Value(value="abc"))
    )
    assert sql == "(id = $1)", sql
    assert params == ["abc"]


def test_compound_and_with_bool_and_str():
    # Mirrors the approval-resolver lookup shape:
    #   (name == "x" AND enabled == True)
    left = Predicate(left=FieldRef(name="name"), op=Op.EQ, right=Value(value="x"))
    right = Predicate(
        left=FieldRef(name="enabled"), op=Op.EQ, right=Value(value=True)
    )
    sql, params = _sql(Predicate(left=left, op=Op.AND, right=right))
    assert sql == "((data->>'name' = $1) AND ((data->>'enabled')::boolean = $2))", sql
    assert params == ["x", True]


def test_contains_renders_jsonb_existence_operator():
    # CONTAINS targets a JSON array field: it must use the jsonb (->)
    # accessor (not text ->>) and the ``?`` existence operator so a GIN
    # index on the expression can back it.
    sql, params = _sql(
        Predicate(left=FieldRef(name="tags"), op=Op.CONTAINS, right=Value(value="x"))
    )
    assert sql == "(data->'tags' ? $1)", sql
    assert params == ["x"]


def test_contains_requires_fieldref_left():
    import pytest

    from primer.model.except_ import BadRequestError

    with pytest.raises(BadRequestError):
        _sql(
            Predicate(
                left=Value(value="x"), op=Op.CONTAINS, right=FieldRef(name="tags")
            )
        )


def test_contains_requires_scalar_value_right():
    import pytest

    from primer.model.except_ import BadRequestError

    with pytest.raises(BadRequestError):
        _sql(
            Predicate(
                left=FieldRef(name="tags"),
                op=Op.CONTAINS,
                right=Value(value=["a", "b"]),
            )
        )


from primer.model.storage import OrderBy
from primer.storage._predicate import render_order_by


def test_like_renders_bare_case_sensitive_operator():
    # Postgres LIKE is case-sensitive by construction; the translator
    # must emit a bare LIKE (no ILIKE, no lower()), so it matches the
    # SQLite backend pinned via PRAGMA case_sensitive_like = ON.
    sql, params = _sql(
        Predicate(left=FieldRef(name="name"), op=Op.LIKE, right=Value(value="Hello%"))
    )
    assert sql == "(data->>'name' LIKE $1)", sql
    assert "ILIKE" not in sql and "lower(" not in sql
    assert params == ["Hello%"]


def test_order_by_nullable_field_is_nulls_last():
    # NULLs sort LAST so keyset pagination can page across the NULL
    # boundary; parity with SQLite's "(field IS NULL) ASC" sort term.
    clause = render_order_by(
        _Model, order_by=[OrderBy(field="name", direction="asc")]
    )
    assert clause == "ORDER BY data->>'name' ASC NULLS LAST, id ASC", clause


def test_order_by_id_has_no_nulls_clause():
    clause = render_order_by(
        _Model, order_by=[OrderBy(field="id", direction="desc")]
    )
    assert clause == "ORDER BY id DESC", clause
