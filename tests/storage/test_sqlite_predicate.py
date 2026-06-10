"""Unit tests for the SQLite predicate translator."""

from __future__ import annotations

import pytest

from primer.model.common import Identifiable
from primer.model.except_ import BadRequestError
from primer.model.storage import FieldRef, Op, OrderBy, Predicate, Value
from primer.storage._sqlite_predicate import (
    _SqlitePredicateTranslator,
    render_order_by_sqlite,
)


class _Row(Identifiable):
    name: str
    count: int
    rate: float
    active: bool


def _T() -> _SqlitePredicateTranslator:
    return _SqlitePredicateTranslator(_Row)


def test_eq_on_text_field():
    t = _T()
    sql, params = t.translate(
        Predicate(left=FieldRef(name="name"), op=Op.EQ, right=Value(value="x"))
    )
    assert sql == "(json_extract(data, '$.name') = ?)"
    assert params == ["x"]


def test_gt_on_int_field_casts():
    t = _T()
    sql, params = t.translate(
        Predicate(left=FieldRef(name="count"), op=Op.GT, right=Value(value=3))
    )
    assert sql == "(CAST(json_extract(data, '$.count') AS INTEGER) > ?)"
    assert params == [3]


def test_le_on_float_field_casts_real():
    t = _T()
    sql, params = t.translate(
        Predicate(left=FieldRef(name="rate"), op=Op.LE, right=Value(value=1.5))
    )
    assert sql == "(CAST(json_extract(data, '$.rate') AS REAL) <= ?)"
    assert params == [1.5]


def test_in_expands_placeholders_with_int_cast():
    t = _T()
    sql, params = t.translate(
        Predicate(
            left=FieldRef(name="count"),
            op=Op.IN,
            right=Value(value=[1, 2, 3]),
        )
    )
    assert sql == "(CAST(json_extract(data, '$.count') AS INTEGER) IN (?, ?, ?))"
    assert params == [1, 2, 3]


def test_in_empty_list_is_false():
    t = _T()
    sql, params = t.translate(
        Predicate(
            left=FieldRef(name="count"), op=Op.IN, right=Value(value=[])
        )
    )
    assert sql == "FALSE"
    assert params == []


def test_and_combines():
    t = _T()
    sql, _ = t.translate(
        Predicate(
            left=Predicate(
                left=FieldRef(name="name"),
                op=Op.EQ,
                right=Value(value="x"),
            ),
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="count"),
                op=Op.GT,
                right=Value(value=0),
            ),
        )
    )
    assert sql == (
        "((json_extract(data, '$.name') = ?) AND "
        "(CAST(json_extract(data, '$.count') AS INTEGER) > ?))"
    )


def test_id_field_uses_pk_column():
    t = _T()
    sql, params = t.translate(
        Predicate(left=FieldRef(name="id"), op=Op.EQ, right=Value(value="abc"))
    )
    assert sql == "(id = ?)"
    assert params == ["abc"]


def test_dotted_path_uses_json_extract():
    class _With(Identifiable):
        meta: dict

    t = _SqlitePredicateTranslator(_With)
    sql, _ = t.translate(
        Predicate(
            left=FieldRef(name="meta.author"),
            op=Op.EQ,
            right=Value(value="alice"),
        )
    )
    assert sql == "(json_extract(data, '$.meta.author') = ?)"


def test_unknown_field_raises_bad_request():
    t = _T()
    with pytest.raises(BadRequestError):
        t.translate(
            Predicate(
                left=FieldRef(name="absent"),
                op=Op.EQ,
                right=Value(value="x"),
            )
        )


def test_is_null_renders_keyword_not_equality():
    """`= NULL` is always UNKNOWN in SQL; Op.IS_NULL MUST render
    `IS NULL` so a row with the field unset (or stored as JSON null)
    actually matches."""
    t = _T()
    sql, params = t.translate(
        Predicate(
            left=FieldRef(name="name"),
            op=Op.IS_NULL,
            # Right operand ignored; passed as a placeholder.
            right=Value(value=None),
        )
    )
    assert sql == "(json_extract(data, '$.name') IS NULL)"
    assert params == []


def test_is_not_null_renders_keyword():
    t = _T()
    sql, params = t.translate(
        Predicate(
            left=FieldRef(name="name"),
            op=Op.IS_NOT_NULL,
            right=Value(value=None),
        )
    )
    assert sql == "(json_extract(data, '$.name') IS NOT NULL)"
    assert params == []


def test_is_null_rejects_non_field_left():
    t = _T()
    with pytest.raises(BadRequestError):
        t.translate(
            Predicate(
                left=Value(value="x"),
                op=Op.IS_NULL,
                right=Value(value=None),
            )
        )


def test_render_order_by_appends_implicit_id_asc():
    clause = render_order_by_sqlite(
        _Row,
        order_by=[OrderBy(field="count", direction="desc")],
    )
    # A non-id key gets a leading "(field IS NULL) ASC" term so NULLs
    # sort LAST (parity with Postgres NULLS LAST), keeping cursor
    # pagination null-safe across the NULL boundary.
    assert clause == (
        "ORDER BY (json_extract(data, '$.count') IS NULL) ASC, "
        "CAST(json_extract(data, '$.count') AS INTEGER) DESC, id ASC"
    )


def test_render_order_by_empty_just_id():
    clause = render_order_by_sqlite(_Row, order_by=None)
    assert clause == "ORDER BY id ASC"
