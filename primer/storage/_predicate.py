"""Predicate-tree -> SQL ``WHERE`` translator for the Postgres Storage backend.

Walks a :class:`primer.model.storage.Predicate` tree and emits a
parametrised SQL fragment plus an ordered list of bind parameters that
asyncpg can dispatch. Field references are resolved through the
target Pydantic model's ``model_fields`` so numeric comparisons get
the appropriate cast (``data->>'count'`` is text in the JSONB
encoding; we cast to ``::bigint`` / ``::double precision`` / etc.
based on the field's declared type).

This module also handles the ``OrderBy`` -> SQL ``ORDER BY`` mapping
because the casting rules are identical: order keys on numeric
columns must cast to keep the index ordering meaningful.

Module-private; consumed only by :mod:`primer.storage.postgres`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from primer.model.except_ import BadRequestError
from primer.model.storage import (
    FieldRef,
    Op,
    OrderBy,
    Predicate,
    Value,
)
from primer.storage._predicate_common import (
    COMPARISON_OPS as _COMPARISON_OPS,
    LOGICAL_OPS as _LOGICAL_OPS,
    PRIMARY_KEY_COLUMN as _PRIMARY_KEY_COLUMN,
    field_annotation as _field_annotation,
    render_null_check as _render_null_check_common,
    render_order_by as _render_order_by_common,
    strip_optional as _strip_optional,
)


# ---------- Field-type resolution -----------------------------------------


def _sql_cast_for(field_type: Any) -> str | None:
    """Return the SQL cast string for a Python type, or None for text-default.

    The JSONB ``->>`` operator always yields text, so numeric / boolean
    comparisons need a cast. ``None`` means "no cast, treat as text" --
    valid for str fields, dotted paths into ``meta``, and the ``id``
    primary key (which is its own column).
    """
    field_type = _strip_optional(field_type)
    if field_type is bool:
        # Check bool BEFORE int -- bool is a subclass of int in Python.
        return "boolean"
    if field_type is int:
        return "bigint"
    if field_type is float:
        return "double precision"
    return None


# ---------- Field expression renderer -------------------------------------


def _quote_jsonb_key(key: str) -> str:
    """Quote a JSONB path key for inline SQL.

    JSONB path keys appear as string literals inside ``->`` /  ``->>``
    expressions; standard SQL string escaping (single quotes, doubled
    to escape) applies. The keys come from Pydantic field names which
    are valid Python identifiers, so the strict subset is safe to
    inline -- but we still escape any embedded single quotes
    defensively.
    """
    escaped = key.replace("'", "''")
    return f"'{escaped}'"


def _render_field_expr(model_class: type[BaseModel], path: str) -> str:
    """SQL expression that yields the field's value as text.

    * ``id`` -> ``id`` (the dedicated PK column)
    * ``"name"`` -> ``data->>'name'``
    * ``"meta.author"`` -> ``data->'meta'->>'author'``
    """
    if path == _PRIMARY_KEY_COLUMN:
        return _PRIMARY_KEY_COLUMN

    parts = path.split(".")
    # Validate top-level field exists; nested paths reach into JSONB freely.
    if parts[0] not in model_class.model_fields:
        raise BadRequestError(
            f"field {path!r} is not declared on model {model_class.__name__!r}"
        )
    expr = "data"
    for inner in parts[:-1]:
        expr += f"->{_quote_jsonb_key(inner)}"
    expr += f"->>{_quote_jsonb_key(parts[-1])}"
    return expr


def _render_jsonb_field_expr(model_class: type[BaseModel], path: str) -> str:
    """SQL expression that yields the field's value as JSONB (not text).

    Identical to :func:`_render_field_expr` but the final hop uses the
    ``->`` (JSONB) accessor instead of ``->>`` (text), so the result can
    feed JSONB operators such as ``?`` / ``@>``. Used by ``CONTAINS``.
    """
    if path == _PRIMARY_KEY_COLUMN:
        raise BadRequestError("CONTAINS is not supported on the primary key")
    parts = path.split(".")
    if parts[0] not in model_class.model_fields:
        raise BadRequestError(
            f"field {path!r} is not declared on model {model_class.__name__!r}"
        )
    expr = "data"
    for inner in parts[:-1]:
        expr += f"->{_quote_jsonb_key(inner)}"
    expr += f"->{_quote_jsonb_key(parts[-1])}"
    return expr


def _render_typed_field_expr(
    model_class: type[BaseModel], path: str
) -> str:
    """Field expression with the appropriate cast applied.

    Used for comparison operators where the operand type matters
    (``>``, ``<``, ``>=``, ``<=``). Equality / LIKE / IN compare on
    the text representation, which is correct for str fields and
    correct-enough for round-trippable scalar JSONB encoding.
    """
    base = _render_field_expr(model_class, path)
    if path == _PRIMARY_KEY_COLUMN:
        return base
    cast = _sql_cast_for(_field_annotation(model_class, path))
    if cast is None:
        return base
    return f"({base})::{cast}"


# ---------- Predicate translator ------------------------------------------


# Comparison operators where a typed cast on the left side is required
# when the field is non-text (bool / int / float).
#
# The JSONB ``->>`` operator always yields text, so a bare
# ``data->>'enabled' = $1`` expression makes asyncpg infer ``$1`` as
# text. asyncpg then strictly rejects a Python ``bool`` / ``int`` /
# ``float`` bind value with ``invalid input for query argument $N:
# ... (expected str, got bool)`` -- it does NOT coerce scalars to text.
# Casting the left side (``(data->>'enabled')::boolean = $1``) makes the
# inferred bind type match the Python value, so EQ / NE must be typed
# too -- not just the four ordering ops. LIKE stays text-only (a cast to
# boolean/numeric would be meaningless and would itself error).
_TYPED_COMPARISON_OPS = {Op.EQ, Op.NE, Op.GT, Op.LT, Op.GE, Op.LE}


class _PredicateTranslator:
    """Walks a :class:`Predicate` tree and emits SQL + bind params.

    Construct one instance per query. The translator accumulates
    parameters in insertion order; ``$1``, ``$2``, ... placeholders in
    the emitted SQL correspond to the returned ``params`` list.
    """

    def __init__(self, model_class: type[BaseModel]) -> None:
        self._model = model_class
        self._params: list[Any] = []

    def translate(self, predicate: Predicate) -> tuple[str, list[Any]]:
        """Compile the predicate tree.

        Returns
        -------
        (sql, params)
            ``sql`` is a parenthesised boolean SQL expression; ``params``
            is the asyncpg-style positional bind list for ``$1..$N``.
        """
        sql = self._render_predicate(predicate)
        return sql, self._params

    def append_param(self, value: Any) -> str:
        """Register a bind parameter and return its ``$N`` placeholder.

        Public so the caller (Storage.list/find) can extend the param
        list when assembling the full query (cursor seeks, LIMIT, etc.).
        """
        self._params.append(value)
        return f"${len(self._params)}"

    # ---------- internals -------------------------------------------------

    def _render(self, node: Predicate | FieldRef | Value) -> str:
        if isinstance(node, Predicate):
            return self._render_predicate(node)
        if isinstance(node, FieldRef):
            return _render_field_expr(self._model, node.name)
        if isinstance(node, Value):
            return self.append_param(node.value)
        raise BadRequestError(f"unknown predicate operand type {type(node).__name__!r}")

    def _render_predicate(self, p: Predicate) -> str:
        if p.op in _LOGICAL_OPS:
            if not isinstance(p.left, Predicate) or not isinstance(p.right, Predicate):
                raise BadRequestError(
                    f"operator {p.op.value!r} requires Predicate operands on both sides"
                )
            left_sql = self._render_predicate(p.left)
            right_sql = self._render_predicate(p.right)
            return f"({left_sql} {_LOGICAL_OPS[p.op]} {right_sql})"

        if p.op == Op.IN:
            return self._render_in(p)

        if p.op == Op.CONTAINS:
            return self._render_contains(p)

        if p.op in (Op.IS_NULL, Op.IS_NOT_NULL):
            return self._render_null_check(p)

        if p.op in _COMPARISON_OPS:
            return self._render_comparison(p)

        raise BadRequestError(f"unsupported operator {p.op.value!r}")

    def _render_null_check(self, p: Predicate) -> str:
        # Shared boilerplate (see _predicate_common.render_null_check); the
        # only dialect input is this backend's field-expression renderer.
        return _render_null_check_common(p, self._model, _render_field_expr)

    def _render_in(self, p: Predicate) -> str:
        if not isinstance(p.left, FieldRef):
            raise BadRequestError("IN requires a FieldRef on the left")
        if not isinstance(p.right, Value) or not isinstance(p.right.value, list):
            raise BadRequestError("IN requires a Value with a list on the right")

        values = p.right.value
        if not values:
            # Empty IN list is always false in standard SQL semantics.
            return "FALSE"

        # Cast the array to match the field's expected scalar type so
        # the planner can use a B-tree / GIN index where one exists.
        field_type = _field_annotation(self._model, p.left.name)
        scalar_cast = _sql_cast_for(field_type) or "text"
        # Choose left-side expression with matching cast.
        if scalar_cast == "text":
            left_sql = _render_field_expr(self._model, p.left.name)
        else:
            left_sql = f"({_render_field_expr(self._model, p.left.name)})::{scalar_cast}"
        placeholder = self.append_param(values)
        return f"({left_sql} = ANY({placeholder}::{scalar_cast}[]))"

    def _render_contains(self, p: Predicate) -> str:
        """Render JSON-array membership as ``data->'field' ? $n``.

        The ``?`` JSONB existence operator returns true when the right
        scalar appears as an element of the array. A GIN index on the
        same ``data->'field'`` expression backs it. asyncpg uses ``$n``
        placeholders, so the literal ``?`` is unambiguous here.
        """
        if not isinstance(p.left, FieldRef):
            raise BadRequestError("CONTAINS requires a FieldRef on the left")
        if not isinstance(p.right, Value) or isinstance(p.right.value, list):
            raise BadRequestError(
                "CONTAINS requires a scalar Value on the right"
            )
        left_sql = _render_jsonb_field_expr(self._model, p.left.name)
        placeholder = self.append_param(p.right.value)
        return f"({left_sql} ? {placeholder})"

    def _render_comparison(self, p: Predicate) -> str:
        sql_op = _COMPARISON_OPS[p.op]

        if isinstance(p.left, FieldRef) and p.op in _TYPED_COMPARISON_OPS:
            left_sql = _render_typed_field_expr(self._model, p.left.name)
        elif isinstance(p.left, FieldRef):
            left_sql = _render_field_expr(self._model, p.left.name)
        else:
            left_sql = self._render(p.left)

        right_sql = self._render(p.right)
        return f"({left_sql} {sql_op} {right_sql})"


# ---------- Order-by translator -------------------------------------------


def render_order_by(
    model_class: type[BaseModel],
    order_by: list[OrderBy] | None,
) -> str:
    """Compile an :class:`OrderBy` list into a SQL ``ORDER BY`` clause.

    Always appends a stable secondary sort by the primary key column
    so cursor pagination has a deterministic seek key. Returns the
    full clause INCLUDING the ``ORDER BY`` keyword (or just
    ``ORDER BY id`` when no order keys are supplied).

    Each non-id key is rendered ``NULLS LAST`` so NULLs sort after all
    non-null values, deterministically and identically to the SQLite
    backend (which emits a ``(field IS NULL) ASC`` sort term). This
    keeps keyset pagination null-safe across a NULL boundary.
    """
    return _render_order_by_common(model_class, order_by, _order_key_terms)


def _order_key_terms(model_class: type[BaseModel], ob: OrderBy) -> list[str]:
    """Postgres sort term(s) for one ORDER BY key.

    Numeric fields cast (so they sort numerically) and non-id keys carry
    ``NULLS LAST``; the id key is a bare directional sort.
    """
    # Use the typed expression so numeric fields sort numerically.
    annotation = _field_annotation(model_class, ob.field)
    cast = _sql_cast_for(annotation)
    if ob.field == _PRIMARY_KEY_COLUMN or cast is None:
        expr = _render_field_expr(model_class, ob.field)
    else:
        expr = f"({_render_field_expr(model_class, ob.field)})::{cast}"
    direction = "ASC" if ob.direction == "asc" else "DESC"
    if ob.field == _PRIMARY_KEY_COLUMN:
        return [f"{expr} {direction}"]
    return [f"{expr} {direction} NULLS LAST"]
