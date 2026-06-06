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
