"""Translate ``--filter k=v`` / ``k=op:v`` expressions into a Predicate tree.

The API's Predicate wire shape is::

    {"kind": "predicate",
     "left": {"kind": "field", "name": ...},
     "op": <Op symbol>,
     "right": {"kind": "value", "value": ...}}

Multiple filters are AND-combined (left-nested), matching the API's binary
Predicate tree.
"""

from __future__ import annotations

from typing import Any


class FilterError(Exception):
    """Raised on a malformed filter expression or unknown operator."""


# named operator -> API Op symbol
_OPS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "lt": "<",
    "ge": ">=",
    "le": "<=",
    "like": "~=",
    "in": "in",
    "contains": "contains",
}


def coerce_value(raw: str) -> Any:
    if raw.startswith("str:"):
        return raw[4:]
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_filter(expr: str) -> tuple[str, str, Any]:
    if "=" not in expr:
        raise FilterError(
            f"bad filter {expr!r}: expected 'field=value' or 'field=op:value'"
        )
    field, _, rest = expr.partition("=")
    field = field.strip()
    if not field:
        raise FilterError(f"bad filter {expr!r}: empty field name")
    op_symbol = "="
    value_str = rest
    if ":" in rest:
        maybe_op, _, after = rest.partition(":")
        if maybe_op in _OPS:
            op_symbol = _OPS[maybe_op]
            value_str = after
        elif maybe_op.isalpha():
            raise FilterError(f"unknown operator {maybe_op!r} in {expr!r}")
    return field, op_symbol, coerce_value(value_str)


def _leaf(field: str, op: str, value: Any) -> dict:
    return {
        "kind": "predicate",
        "left": {"kind": "field", "name": field},
        "op": op,
        "right": {"kind": "value", "value": value},
    }


def build_predicate(filters: list[str]) -> dict | None:
    leaves = [_leaf(*parse_filter(f)) for f in filters]
    if not leaves:
        return None
    acc = leaves[0]
    for leaf in leaves[1:]:
        acc = {"kind": "predicate", "left": acc, "op": "and", "right": leaf}
    return acc
