"""JSON ops: the alternative input to Canvas Lang, and the normalized form records keep.

An agent may send ``ops`` instead of ``source``: a list of the five operations
below. They become the same statements the parser produces, so the same rules
judge them (only the error unit changes, to "op N"). ``statements_to_ops`` goes
the other way and gives the normalized ops a ``canvas_op`` record stores. Pure.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError

from primer.canvas.catalog import CATALOG
from primer.canvas.lang import (
    KEEP,
    MAX_BATCH_STATEMENTS,
    MAX_ERRORS,
    Append,
    Assign,
    Clear,
    Delete,
    LangError,
    Ref,
    SetProp,
    Statement,
    clip,
)
from primer.canvas.state import normalize_assign

#: The most characters of Canvas Lang source one batch may hold.
MAX_SOURCE_CHARS = 600_000
#: The most characters of compact JSON the ops of one batch may hold: the same bound as the source, so the two inputs of a batch are
#: capped alike (a canvas holds at most 512 KiB, and the batch validator judges what is left after this).
MAX_OPS_CHARS = 600_000


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetOp(_Op):
    """Create or replace an element: ``id = Type(...)``."""

    op: Literal["set"]
    id: str
    type: str
    props: dict[str, Any] = Field(default_factory=dict)
    children: list[str] | None = None


class PropOp(_Op):
    """Set one property (``null`` removes it): ``id.prop = value``."""

    op: Literal["prop"]
    id: str
    prop: str
    value: Any


class AppendOp(_Op):
    """Append children to a container: ``id += [ids]``."""

    op: Literal["append"]
    id: str
    children: list[str]


class DeleteOp(_Op):
    """Remove elements and their subtrees: ``del ids``."""

    op: Literal["delete"]
    ids: list[str]


class ClearOp(_Op):
    """Remove everything but an empty root: ``clear``."""

    op: Literal["clear"]


CanvasOp = Annotated[SetOp | PropOp | AppendOp | DeleteOp | ClearOp, Field(discriminator="op")]

#: What each op looks like, as the "expected" of an op that is not one (the twin of ``catalog.signature`` for a Canvas Lang line).
OP_SHAPES = (
    '{"op":"set","id","type","props"?,"children"?}',
    '{"op":"prop","id","prop","value"}',
    '{"op":"append","id","children"}',
    '{"op":"delete","ids"}',
    '{"op":"clear"}',
)
_OP_NAMES = "set, prop, append, delete or clear"
_MAX_PROBLEMS_PER_OP = 3


def ensure_ops_fit(raw: Any) -> Any:
    """A ``mode="before"`` validator for a field of ops: refuse a list that holds more than ``MAX_BATCH_STATEMENTS`` ops or
    ``MAX_OPS_CHARS`` characters of compact JSON.

    It runs on the raw value, before pydantic builds a model for each op, so an oversized batch costs one serialisation and nothing more.
    ``source`` is capped the same way (``MAX_BATCH_STATEMENTS`` and ``MAX_SOURCE_CHARS``). ``None`` passes: the field is optional.
    """
    if raw is None:
        return raw
    if isinstance(raw, (list, tuple)) and len(raw) > MAX_BATCH_STATEMENTS:
        raise PydanticCustomError(
            "ops_too_many",
            "a batch holds at most {limit} ops; got {count}",
            {"count": len(raw), "limit": MAX_BATCH_STATEMENTS},
        )
    try:
        size = len(json.dumps(raw, separators=(",", ":"), ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        raise PydanticCustomError("ops_not_json", "ops must be a list of JSON objects") from None
    if size > MAX_OPS_CHARS:
        raise PydanticCustomError(
            "ops_too_large", "ops hold {size} characters of JSON; the limit is {limit}", {"size": size, "limit": MAX_OPS_CHARS},
        )
    return raw


def _op_problem(error: Any) -> str:
    """One pydantic error of an op, in words that echo none of the input but the (clipped) name of the field."""
    loc = error["loc"]
    if error["type"] in ("union_tag_invalid", "union_tag_not_found"):
        return f"op must be one of {_OP_NAMES}"
    path = ".".join(clip(str(part)) for part in loc[3:])
    return f"{path}: {error['msg']}" if path else str(error["msg"])


def ops_errors(exc: ValidationError) -> list[LangError] | None:
    """The malformed ops behind ``exc`` as one error each (``unit="op"``, op numbers from 1), or ``None`` when some error is not about one op.

    ``exc`` comes from validating a model with ``ops: list[CanvasOp]``. Each error names its op, says what is wrong with it (the first
    three problems) and carries ``OP_SHAPES`` as the expected form, like a Canvas Lang line that names its signature. At most
    ``MAX_ERRORS`` ops are listed, then the rest are counted. The messages echo no input: pydantic's own text is replaced where it would.
    """
    problems: dict[int, list[str]] = {}
    for error in exc.errors():
        loc = error["loc"]
        if len(loc) < 2 or loc[0] != "ops" or not isinstance(loc[1], int):
            return None
        problems.setdefault(loc[1] + 1, []).append(_op_problem(error))
    expected = " | ".join(OP_SHAPES)
    errors: list[LangError] = []
    dropped = 0
    dropped_op = 0
    for number, found in sorted(problems.items()):
        if len(errors) >= MAX_ERRORS:
            dropped_op = dropped_op or number
            dropped += 1
            continue
        shown = "; ".join(found[:_MAX_PROBLEMS_PER_OP])
        if len(found) > _MAX_PROBLEMS_PER_OP:
            shown += f"; and {len(found) - _MAX_PROBLEMS_PER_OP} more problems"
        errors.append(LangError(number, shown, expected, "op"))
    if dropped:
        errors.append(LangError(dropped_op, f"{dropped} more malformed ops are not shown", None, "op"))
    return errors


def _refs_if_ids(value: Any) -> Any:
    """A list of strings is a list of references when it is a container's children."""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [Ref(v) for v in value]
    return value


def ops_to_statements(ops: Sequence[CanvasOp]) -> list[Statement]:
    """Op ``n`` (counting from 1) becomes a statement on line ``n``.

    A ``set`` becomes an ``Assign`` whose children list is the first argument of a
    container and whose props are named arguments.
    """
    statements: list[Statement] = []
    for n, op in enumerate(ops, start=1):
        if isinstance(op, SetOp):
            kwargs = list(op.props.items())
            refs = [Ref(child) for child in op.children] if op.children is not None else None
            spec = CATALOG.get(op.type)
            if refs is not None and spec is not None and spec.container:
                statements.append(Assign(n, op.id, op.type, (refs,), tuple(kwargs)))
            else:
                if refs is not None:
                    kwargs.append(("children", refs))
                statements.append(Assign(n, op.id, op.type, (), tuple(kwargs)))
        elif isinstance(op, PropOp):
            value = _refs_if_ids(op.value) if op.prop == "children" else op.value
            statements.append(SetProp(n, op.id, op.prop, value))
        elif isinstance(op, AppendOp):
            statements.append(Append(n, op.id, tuple(op.children)))
        elif isinstance(op, DeleteOp):
            statements.append(Delete(n, tuple(op.ids)))
        elif isinstance(op, ClearOp):
            statements.append(Clear(n))
        else:
            raise TypeError(f"not a canvas op: {op!r}")
    return statements


def _plain(value: Any) -> Any:
    """A value as plain JSON: references become their ids and tuples become lists."""
    if isinstance(value, Ref):
        return value.id
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return copy.deepcopy(value)


def statements_to_ops(statements: Sequence[Statement]) -> list[dict[str, Any]]:
    """The normalized ops for statements that were applied (what a ``canvas_op`` record stores).

    A ``set`` carries the element as the canvas keeps it: its props with the
    defaults left out and, for a container, its children. An argument given as
    ``...`` is left out (it changed nothing), and so is a ``prop`` op whose value
    is ``...``: a recorded ``set`` for ``Html(..., 80)`` carries no html and cannot be
    replayed, and replaying history is out of scope (design 8.7). Raises ValueError for
    a statement that cannot be named (an unknown type, property or too many
    arguments); such a batch would not have applied.
    """
    ops: list[dict[str, Any]] = []
    for st in statements:
        if isinstance(st, Assign):
            spec = CATALOG.get(st.type)
            if spec is None:
                raise ValueError(f"unknown element type '{clip(st.type)}'")
            props, children = normalize_assign(spec, st.args, st.kwargs)
            op: dict[str, Any] = {"op": "set", "id": st.id, "type": st.type, "props": props}
            if spec.container and children is not None:
                op["children"] = children
            ops.append(op)
        elif isinstance(st, SetProp):
            if st.value is KEEP:
                continue
            ops.append({"op": "prop", "id": st.id, "prop": st.prop, "value": _plain(st.value)})
        elif isinstance(st, Append):
            ops.append({"op": "append", "id": st.id, "children": list(st.children)})
        elif isinstance(st, Delete):
            ops.append({"op": "delete", "ids": list(st.ids)})
        elif isinstance(st, Clear):
            ops.append({"op": "clear"})
        else:
            raise TypeError(f"not a Canvas Lang statement: {st!r}")
    return ops


__all__ = [
    "MAX_BATCH_STATEMENTS",
    "MAX_OPS_CHARS",
    "MAX_SOURCE_CHARS",
    "OP_SHAPES",
    "AppendOp",
    "CanvasOp",
    "ClearOp",
    "DeleteOp",
    "PropOp",
    "SetOp",
    "ensure_ops_fit",
    "ops_errors",
    "ops_to_statements",
    "statements_to_ops",
]
