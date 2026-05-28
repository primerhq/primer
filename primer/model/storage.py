"""Data types for the storage abstraction layer.

Carries the request/response shapes and predicate trees that
:class:`matrix.int.Storage` consumes and produces. No storage logic ships
in this module -- backend adapters live alongside the ABC and translate
these types to their native query shape (SQL, MongoDB, in-memory dict,
etc.).

The three concept groups exported here:

* **Predicate tree** -- :class:`Predicate`, :class:`FieldRef`,
  :class:`Value`, :class:`Op`. A binary expression tree the search
  layer evaluates against stored entities.
* **Pagination** -- :class:`OffsetPage` and :class:`CursorPage` request
  shapes, :class:`OffsetPageResponse` and :class:`CursorPageResponse`
  response shapes. Discriminated on ``kind`` so requests and responses
  serialise cleanly through JSON.
* **Ordering** -- :class:`OrderBy`, used by ``list`` / ``find`` to
  produce a stable order across pages.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Generic, Literal, TypeVar, Union

from pydantic import BaseModel, Field


# ===========================================================================
# Predicate tree
# ===========================================================================


class Op(str, Enum):
    """Predicate operators.

    Comparison operators (``EQ``, ``NE``, ``LIKE``, ``GT``, ``LT``, ``GE``,
    ``LE``) compare two operands; the canonical shape is
    :class:`FieldRef` on the left and :class:`Value` on the right, but
    backends MAY accept :class:`FieldRef` on both sides for column-vs-
    column comparison. Backends that can't translate a given operand
    layout should raise :class:`matrix.model.except_.BadRequestError`.

    ``IN`` expects a :class:`FieldRef` on the left and a :class:`Value`
    on the right whose ``value`` is a list of scalars. Matches when
    the field's value equals any element of the list. Backends MAY
    optimise to a native IN clause (SQL ``IN``, MongoDB ``$in``) but
    MUST evaluate the same set-membership semantics.

    Logical operators (``AND``, ``OR``) require :class:`Predicate` on
    both sides. The tree is binary; for multi-operand expressions, nest:
    ``a AND b AND c`` -> ``Predicate(AND, Predicate(AND, a, b), c)``.

    ``LIKE`` follows SQL semantics: ``%`` matches any sequence,
    ``_`` matches a single character. Backends SHOULD translate to their
    native pattern syntax.
    """

    EQ = "="
    NE = "!="
    LIKE = "~="
    GT = ">"
    LT = "<"
    GE = ">="
    LE = "<="
    IN = "in"
    AND = "and"
    OR = "or"


# Scalar value types accepted in :class:`Value`. Constrained to JSON-
# compatible primitives so the tree round-trips through any wire format.
_ScalarValue = Union[str, int, float, bool, None]

# Value operand contents: a single scalar OR a list of scalars (for
# :attr:`Op.IN`). Backends evaluating IN expect the right operand to
# be a :class:`Value` whose ``value`` is a list; non-IN operators
# expect a scalar.
_OperandValue = Union[_ScalarValue, list[_ScalarValue]]


class FieldRef(BaseModel):
    """Reference to a field on the stored entity.

    ``name`` is the Pydantic attribute name on the model. Dotted paths
    (e.g. ``"meta.author"``) are permitted for nested fields; backend
    support for nesting varies (SQL JSON paths, MongoDB dot notation).
    Backends that can't translate a path should raise
    :class:`matrix.model.except_.BadRequestError` rather than silently
    misinterpreting.
    """

    kind: Literal["field"] = Field(
        default="field",
        description="Discriminator tag identifying this operand as a field reference.",
    )
    name: str = Field(
        ...,
        min_length=1,
        description=(
            "Field name on the entity. Dotted paths (e.g. 'meta.author') "
            "are permitted for nested access; backend support varies."
        ),
    )


class Value(BaseModel):
    """Literal value used as a predicate operand.

    Constrained to JSON-compatible scalars (or a list of scalars) so
    the predicate tree serialises losslessly. The list form is used
    only with :attr:`Op.IN` -- the right operand of an IN expression
    is a :class:`Value` whose ``value`` is the list of accepted
    matches; for every other operator the value is a single scalar.
    """

    kind: Literal["value"] = Field(
        default="value",
        description="Discriminator tag identifying this operand as a literal value.",
    )
    value: _OperandValue = Field(
        ...,
        description=(
            "The literal value: a scalar (str, int, float, bool, None) "
            "for comparison operators, or a list of scalars when used "
            "as the right operand of Op.IN."
        ),
    )


class Predicate(BaseModel):
    """One node in the search-expression tree.

    Trees are strictly binary: each node has exactly one ``left`` and
    one ``right`` operand. The ``op`` selects the operator. Nest to
    express multi-operand AND/OR.

    Construction example::

        # name = "kb-1" AND id != "doc-removed"
        Predicate(
            left=Predicate(
                left=FieldRef(name="name"),
                op=Op.EQ,
                right=Value(value="kb-1"),
            ),
            op=Op.AND,
            right=Predicate(
                left=FieldRef(name="id"),
                op=Op.NE,
                right=Value(value="doc-removed"),
            ),
        )
    """

    kind: Literal["predicate"] = Field(
        default="predicate",
        description="Discriminator tag identifying this operand as a sub-predicate.",
    )
    left: "Operand" = Field(
        ...,
        description="Left operand: another Predicate, a FieldRef, or a Value.",
    )
    op: Op = Field(
        ...,
        description="Operator joining the two operands.",
    )
    right: "Operand" = Field(
        ...,
        description="Right operand: another Predicate, a FieldRef, or a Value.",
    )


Operand = Annotated[
    Union[Predicate, FieldRef, Value],
    Field(discriminator="kind"),
]
"""Type alias: an operand on either side of a :class:`Predicate`.

Discriminated by the ``kind`` field so Pydantic can parse the operand
from an untyped dict (e.g. a JSON request body) without ambiguity.
"""


# Resolve the forward reference inside Predicate now that Operand exists.
Predicate.model_rebuild()


# ===========================================================================
# Ordering
# ===========================================================================


class OrderBy(BaseModel):
    """Sort key for ``list`` / ``find`` results.

    Multiple :class:`OrderBy` entries are applied left-to-right (first
    is primary sort key). Cursor-style pagination relies on a stable
    total ordering; if the supplied ``order_by`` is not unique, the
    backend MUST add an implicit secondary sort by ``id`` to keep page
    boundaries deterministic.
    """

    field: str = Field(
        ...,
        min_length=1,
        description="Field name to sort by (dotted paths permitted).",
    )
    direction: Literal["asc", "desc"] = Field(
        default="asc",
        description="Sort direction. Defaults to ascending.",
    )


# ===========================================================================
# Pagination -- requests
# ===========================================================================


class OffsetPage(BaseModel):
    """Offset+length pagination request.

    Equivalent to SQL ``LIMIT length OFFSET offset``. Cheap on backends
    with native offset support; emulated by skip-and-take on others.
    """

    kind: Literal["offset"] = Field(
        default="offset",
        description="Discriminator tag identifying this request as offset-based.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of items to skip from the start of the result set.",
    )
    length: int = Field(
        ...,
        ge=1,
        le=200,
        description="Maximum number of items to return (1..200, spec §4).",
    )


class CursorPage(BaseModel):
    """Cursor-based pagination request.

    Set ``cursor=None`` for the first page. Pass the ``next_cursor``
    returned by the previous response to fetch the next page. The
    cursor is opaque to the caller -- backends choose its encoding
    (encoded primary key, base64 row id, etc.).
    """

    kind: Literal["cursor"] = Field(
        default="cursor",
        description="Discriminator tag identifying this request as cursor-based.",
    )
    cursor: str | None = Field(
        default=None,
        description="Opaque cursor from the previous response, or None for the first page.",
    )
    length: int = Field(
        ...,
        ge=1,
        le=200,
        description="Maximum number of items to return (1..200, spec §4).",
    )


PageRequest = Annotated[
    Union[OffsetPage, CursorPage],
    Field(discriminator="kind"),
]
"""Type alias: a pagination request, either offset-based or cursor-based.

Discriminated by ``kind`` so requests parse cleanly from JSON.
"""


# ===========================================================================
# Pagination -- responses
# ===========================================================================


_ItemT = TypeVar("_ItemT", bound=BaseModel)


class OffsetPageResponse(BaseModel, Generic[_ItemT]):
    """Response to an :class:`OffsetPage` request.

    ``length`` is the actual count returned (may be less than the
    requested length on the final page). ``total`` is the total count
    of all entities matching the request; backends that cannot supply
    a total cheaply MAY return ``None`` rather than fabricate one.
    """

    kind: Literal["offset"] = Field(
        default="offset",
        description="Discriminator tag identifying this response as offset-based.",
    )
    offset: int = Field(
        ...,
        ge=0,
        description="The offset that was requested (echoed for client convenience).",
    )
    length: int = Field(
        ...,
        ge=0,
        description="Actual number of items returned in this page.",
    )
    total: int | None = Field(
        default=None,
        description=(
            "Total number of matching entities. None if the backend "
            "cannot produce a total count cheaply."
        ),
    )
    items: list[_ItemT] = Field(
        ...,
        description="The page of entities, in the requested order.",
    )


class CursorPageResponse(BaseModel, Generic[_ItemT]):
    """Response to a :class:`CursorPage` request.

    ``next_cursor`` is ``None`` when the iteration is exhausted.
    Otherwise the caller passes it back as the next request's
    ``cursor`` to continue.
    """

    kind: Literal["cursor"] = Field(
        default="cursor",
        description="Discriminator tag identifying this response as cursor-based.",
    )
    next_cursor: str | None = Field(
        default=None,
        description="Cursor for the next page, or None if no more pages.",
    )
    items: list[_ItemT] = Field(
        ...,
        description="The page of entities, in the requested order.",
    )


# A ``PageResponse[T]`` would naturally be
# ``OffsetPageResponse[T] | CursorPageResponse[T]``, but Python type
# aliases parameterised by a TypeVar require the consumer (the storage
# ABC) to write the union explicitly with its own bound. We export the
# two response classes; consumers form the union at use site.


__all__ = [
    "CursorPage",
    "CursorPageResponse",
    "FieldRef",
    "OffsetPage",
    "OffsetPageResponse",
    "Op",
    "Operand",
    "OrderBy",
    "PageRequest",
    "Predicate",
    "Value",
]
