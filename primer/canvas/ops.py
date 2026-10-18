"""JSON ops: the alternative input to Canvas Lang, and the normalized form records keep.

An agent may send ``ops`` instead of ``source``: a list of the five operations
below. They become the same statements the parser produces, so the same rules
judge them (only the error unit changes, to "op N"). ``statements_to_ops`` goes
the other way and gives the normalized ops a ``canvas_op`` record stores. Pure.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from primer.canvas.catalog import CATALOG
from primer.canvas.lang import KEEP, Append, Assign, Clear, Delete, Ref, SetProp, Statement
from primer.canvas.state import normalize_assign


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
    is ``...``. Raises ValueError for a statement that cannot be named (an unknown
    type, property or too many arguments); such a batch would not have applied.
    """
    ops: list[dict[str, Any]] = []
    for st in statements:
        if isinstance(st, Assign):
            spec = CATALOG.get(st.type)
            if spec is None:
                raise ValueError(f"unknown element type '{st.type}'")
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
    "AppendOp",
    "CanvasOp",
    "ClearOp",
    "DeleteOp",
    "PropOp",
    "SetOp",
    "ops_to_statements",
    "statements_to_ops",
]
