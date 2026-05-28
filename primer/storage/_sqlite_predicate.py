"""Predicate-tree -> SQLite SQL translator.

Sibling of :mod:`primer.storage._predicate` (the Postgres translator).
Emits ``json_extract(data, '$.path')`` for field references,
``CAST(... AS INTEGER|REAL)`` for numeric comparisons, ``?`` as the
positional placeholder, and ``IN (?, ?, ...)`` expanded inline for
:class:`Op.IN`.

Module-private; consumed only by :mod:`primer.storage.sqlite`.
"""

from __future__ import annotations

import types
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from primer.model.except_ import BadRequestError
from primer.model.storage import (
    FieldRef,
    Op,
    OrderBy,
    Predicate,
    Value,
)


# ---------- Field-type resolution ----------------------------------------


def _strip_optional(tp: Any) -> Any:
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _sqlite_cast_for(field_type: Any) -> str | None:
    """Map a Python annotation to a SQLite CAST target, or None for TEXT."""
    field_type = _strip_optional(field_type)
    if field_type is bool:
        # SQLite stores Python bools as 1 / 0 in the JSON blob; for
        # equality comparisons against a Python bool the placeholder
        # arrives as int (sqlite3 adapter), and json_extract returns
        # int. No cast needed.
        return None
    if field_type is int:
        return "INTEGER"
    if field_type is float:
        return "REAL"
    return None


def _field_annotation(model_class: type[BaseModel], path: str) -> Any:
    parts = path.split(".")
    if len(parts) > 1:
        return Any
    field = model_class.model_fields.get(parts[0])
    if field is None:
        raise BadRequestError(
            f"field {path!r} is not declared on model {model_class.__name__!r}"
        )
    return field.annotation


# ---------- Field-expression renderer ------------------------------------


_PRIMARY_KEY_COLUMN = "id"


def _render_field_expr(model_class: type[BaseModel], path: str) -> str:
    """SQL expression that yields the field's value (no cast)."""
    if path == _PRIMARY_KEY_COLUMN:
        return _PRIMARY_KEY_COLUMN
    parts = path.split(".")
    if parts[0] not in model_class.model_fields:
        raise BadRequestError(
            f"field {path!r} is not declared on model {model_class.__name__!r}"
        )
    json_path = "$." + ".".join(parts)
    # SQLite JSON path keys use $.a.b notation. Field names are valid
    # Python identifiers, so they're safe to inline; we still escape
    # single quotes defensively in case a future model uses a name
    # with one.
    json_path = json_path.replace("'", "''")
    return f"json_extract(data, '{json_path}')"


def _render_typed_field_expr(model_class: type[BaseModel], path: str) -> str:
    """Field expression with the appropriate CAST applied for ordering ops."""
    base = _render_field_expr(model_class, path)
    if path == _PRIMARY_KEY_COLUMN:
        return base
    cast = _sqlite_cast_for(_field_annotation(model_class, path))
    if cast is None:
        return base
    return f"CAST({base} AS {cast})"


# ---------- Translator ---------------------------------------------------


_COMPARISON_OPS: dict[Op, str] = {
    Op.EQ: "=",
    Op.NE: "!=",
    Op.LIKE: "LIKE",
    Op.GT: ">",
    Op.LT: "<",
    Op.GE: ">=",
    Op.LE: "<=",
}

_LOGICAL_OPS: dict[Op, str] = {
    Op.AND: "AND",
    Op.OR: "OR",
}

_TYPED_COMPARISON_OPS = {Op.GT, Op.LT, Op.GE, Op.LE}


class _SqlitePredicateTranslator:
    """Walks a :class:`Predicate` and emits SQLite SQL + bind params."""

    def __init__(self, model_class: type[BaseModel]) -> None:
        self._model = model_class
        self._params: list[Any] = []

    def translate(self, predicate: Predicate) -> tuple[str, list[Any]]:
        sql = self._render_predicate(predicate)
        return sql, self._params

    def append_param(self, value: Any) -> str:
        """Register a bind param; returns ``?``."""
        self._params.append(value)
        return "?"

    # ----- internals -----------------------------------------------------

    def _render_predicate(self, p: Predicate) -> str:
        if p.op in _LOGICAL_OPS:
            if not isinstance(p.left, Predicate) or not isinstance(p.right, Predicate):
                raise BadRequestError(
                    f"operator {p.op.value!r} requires Predicate operands on both sides"
                )
            return (
                f"({self._render_predicate(p.left)} "
                f"{_LOGICAL_OPS[p.op]} "
                f"{self._render_predicate(p.right)})"
            )
        if p.op == Op.IN:
            return self._render_in(p)
        if p.op in _COMPARISON_OPS:
            return self._render_comparison(p)
        raise BadRequestError(f"unsupported operator {p.op.value!r}")

    def _render_in(self, p: Predicate) -> str:
        if not isinstance(p.left, FieldRef):
            raise BadRequestError("IN requires a FieldRef on the left")
        if not isinstance(p.right, Value) or not isinstance(p.right.value, list):
            raise BadRequestError("IN requires a Value with a list on the right")
        values = p.right.value
        if not values:
            return "FALSE"
        field_type = _field_annotation(self._model, p.left.name)
        cast = _sqlite_cast_for(field_type)
        if cast is None:
            left_sql = _render_field_expr(self._model, p.left.name)
        else:
            left_sql = f"CAST({_render_field_expr(self._model, p.left.name)} AS {cast})"
        placeholders = ", ".join(self.append_param(v) for v in values)
        return f"({left_sql} IN ({placeholders}))"

    def _render_comparison(self, p: Predicate) -> str:
        sql_op = _COMPARISON_OPS[p.op]
        if isinstance(p.left, FieldRef) and p.op in _TYPED_COMPARISON_OPS:
            left_sql = _render_typed_field_expr(self._model, p.left.name)
        elif isinstance(p.left, FieldRef):
            left_sql = _render_field_expr(self._model, p.left.name)
        elif isinstance(p.left, Value):
            left_sql = self.append_param(p.left.value)
        else:
            raise BadRequestError(
                "comparison left side must be FieldRef or Value"
            )
        if isinstance(p.right, FieldRef):
            right_sql = _render_field_expr(self._model, p.right.name)
        elif isinstance(p.right, Value):
            right_sql = self.append_param(p.right.value)
        else:
            raise BadRequestError(
                "comparison right side must be FieldRef or Value"
            )
        return f"({left_sql} {sql_op} {right_sql})"


# ---------- Order-by renderer --------------------------------------------


def render_order_by_sqlite(
    model_class: type[BaseModel],
    order_by: list[OrderBy] | None,
) -> str:
    """Compile :class:`OrderBy` keys into a SQLite ``ORDER BY`` clause.

    Always appends an implicit ``id ASC`` tiebreaker so cursor
    pagination has a deterministic seek key.
    """
    parts: list[str] = []
    seen_id = False
    for ob in order_by or []:
        if ob.field == _PRIMARY_KEY_COLUMN:
            seen_id = True
        annotation = _field_annotation(model_class, ob.field)
        cast = _sqlite_cast_for(annotation)
        if ob.field == _PRIMARY_KEY_COLUMN or cast is None:
            expr = _render_field_expr(model_class, ob.field)
        else:
            expr = f"CAST({_render_field_expr(model_class, ob.field)} AS {cast})"
        direction = "ASC" if ob.direction == "asc" else "DESC"
        parts.append(f"{expr} {direction}")
    if not seen_id:
        parts.append(f"{_PRIMARY_KEY_COLUMN} ASC")
    return "ORDER BY " + ", ".join(parts)


__all__ = [
    "_PRIMARY_KEY_COLUMN",
    "_SqlitePredicateTranslator",
    "_render_field_expr",
    "_render_typed_field_expr",
    "render_order_by_sqlite",
]
