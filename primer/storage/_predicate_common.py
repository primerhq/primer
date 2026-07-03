"""Dialect-independent helpers shared by the two predicate translators.

:mod:`primer.storage._predicate` (Postgres) and
:mod:`primer.storage._sqlite_predicate` (SQLite) keep SEPARATE renderers — the
parts that genuinely diverge (placeholders, casts, ``IN`` expansion,
``CONTAINS``, EQ/NE typing, and the per-key NULL-ordering SQL) do not unify
without over-abstracting. This module holds only the boilerplate that is
byte-identical across both: annotation unwrapping, field-annotation resolution,
the primary-key column name, the comparison / logical operator maps, the
``IS NULL`` / ``IS NOT NULL`` renderer, and the ``ORDER BY`` assembly skeleton.

Behaviour parity between the two dialects is guarded by the parametrised
predicate contract test.

Module-private; consumed only by the two sibling translator modules.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from primer.model.except_ import BadRequestError
from primer.model.storage import FieldRef, Op, OrderBy, Predicate


# The PRIMARY KEY column. ``id`` is hoisted out of the JSON(B) document into
# its own column so it can be the table's primary key and carry the obvious
# B-tree index.
PRIMARY_KEY_COLUMN = "id"


# ---------- Field-type resolution -----------------------------------------


def strip_optional(tp: Any) -> Any:
    """Unwrap ``T | None`` / ``Optional[T]`` to ``T``.

    Works for both ``Union[X, None]`` and the PEP 604 ``X | None`` form.
    """
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def field_annotation(model_class: type[BaseModel], path: str) -> Any:
    """Resolve the annotation for a (possibly dotted) field path.

    For top-level fields this is ``model_class.model_fields[name].annotation``.
    For dotted paths (e.g. ``"meta.author"``) the inner type cannot be known
    statically (Pydantic's ``dict[str, Any]`` carries no structural info) — so
    return ``Any`` and the caller skips casting.
    """
    parts = path.split(".")
    if len(parts) > 1:
        return Any
    field = model_class.model_fields.get(parts[0])
    if field is None:
        raise BadRequestError(
            f"field {path!r} is not declared on model {model_class.__name__!r}"
        )
    return field.annotation


# ---------- Operator maps --------------------------------------------------


# Comparison operator -> SQL keyword. Identical in both dialects.
COMPARISON_OPS: dict[Op, str] = {
    Op.EQ: "=",
    Op.NE: "!=",
    Op.LIKE: "LIKE",
    Op.GT: ">",
    Op.LT: "<",
    Op.GE: ">=",
    Op.LE: "<=",
}

# Logical operator -> SQL keyword. Identical in both dialects.
LOGICAL_OPS: dict[Op, str] = {
    Op.AND: "AND",
    Op.OR: "OR",
}


# ---------- Shared renderers ----------------------------------------------


def render_null_check(
    p: Predicate,
    model_class: type[BaseModel],
    render_field_expr: Callable[[type[BaseModel], str], str],
) -> str:
    """Render ``IS NULL`` / ``IS NOT NULL`` against a FieldRef.

    The right operand is ignored — the canonical caller passes a placeholder
    ``Value`` so the Operand union stays satisfied; the value never reaches
    SQL. ``render_field_expr`` is the dialect's field-expression renderer so
    the emitted left side matches the backend.
    """
    if not isinstance(p.left, FieldRef):
        raise BadRequestError(
            f"operator {p.op.value!r} requires a FieldRef on the left"
        )
    left_sql = render_field_expr(model_class, p.left.name)
    keyword = "IS NULL" if p.op == Op.IS_NULL else "IS NOT NULL"
    return f"({left_sql} {keyword})"


def render_order_by(
    model_class: type[BaseModel],
    order_by: list[OrderBy] | None,
    render_key: Callable[[type[BaseModel], OrderBy], list[str]],
) -> str:
    """Assemble an ``ORDER BY`` clause from per-key sort terms.

    Shared skeleton across both backends: iterate the keys, always append a
    stable ``id ASC`` tiebreaker when the caller did not already order by the
    primary key (so cursor pagination has a deterministic seek key), and prefix
    the ``ORDER BY`` keyword. ``render_key`` is the dialect-specific renderer
    returning the one-or-more SQL sort terms for a single key — that is where
    the null-last emission and cast syntax diverge and stay per-dialect.
    """
    parts: list[str] = []
    seen_id = False
    for ob in order_by or []:
        if ob.field == PRIMARY_KEY_COLUMN:
            seen_id = True
        parts.extend(render_key(model_class, ob))
    if not seen_id:
        parts.append(f"{PRIMARY_KEY_COLUMN} ASC")
    return "ORDER BY " + ", ".join(parts)


__all__ = [
    "COMPARISON_OPS",
    "LOGICAL_OPS",
    "PRIMARY_KEY_COLUMN",
    "field_annotation",
    "render_null_check",
    "render_order_by",
    "strip_optional",
]
