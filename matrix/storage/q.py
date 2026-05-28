"""Type-safe predicate builder for the Matrix storage layer.

``Q[ModelT]`` validates every field name against ``model_cls.model_fields``
at call time, making SQL-injection-via-field-name structurally impossible for
callers that use ``Q`` (field names in the predicate ADT are interpolated
directly into SQL; values go through asyncpg / aiosqlite parameter binding
and are already safe).

Usage::

    from matrix.storage.q import Q
    from matrix.model.workspace_session import WorkspaceSession

    predicate = (
        Q(WorkspaceSession)
        .where("workspace_id", wid)
        .where("status", "active")
        .build()
    )

Unknown fields raise ``KeyError`` immediately:

    Q(WorkspaceSession).where("typo_field", wid)
    # → KeyError: "unknown field 'typo_field' on WorkspaceSession; known: [...]"

Dotted JSONB paths (e.g. ``"meta.author"``) are also validated: the top-level
segment must be in ``model_fields``, and every subsequent segment must match
the safe identifier pattern ``^[A-Za-z_][A-Za-z0-9_]*$``.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from pydantic import BaseModel

from matrix.model.storage import FieldRef, Op, Predicate, Value

# Regex for safe JSONB path segments (Python identifier characters only).
_SAFE_SEGMENT: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Q[ModelT: BaseModel]:
    """Type-safe predicate builder.

    Field names are validated against ``model_cls.model_fields`` so they
    cannot be passed in dynamically from user input — a misspelled or
    attacker-supplied field name fails at ``Q.where()`` call time, not
    at SQL-execution time.  Values are passed via the existing
    ``Value(value=...)`` wrapper which goes through asyncpg / aiosqlite
    parameter binding (already safe).

    ``or_`` is a classmethod that composes multiple ``Q`` instances with
    logical OR.  All supplied ``Q`` instances must share the same
    ``model_cls``; a mismatch raises ``TypeError``.

    Parameters
    ----------
    model_cls:
        The Pydantic model class whose ``model_fields`` defines the
        allowed field names.
    """

    def __init__(self, model_cls: type[ModelT]) -> None:
        self._model_cls = model_cls
        self._fields: frozenset[str] = frozenset(model_cls.model_fields)
        self._predicates: list[Predicate] = []

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _check_field(self, field: str) -> None:
        """Validate *field* against the model's declared fields.

        Accepts dotted paths such as ``"data.parked_status"``: the
        top-level segment must exist in ``model_fields``; each subsequent
        segment must match ``^[A-Za-z_][A-Za-z0-9_]*$``.

        Raises
        ------
        KeyError
            When the top-level field is not declared on the model.
        ValueError
            When a dotted path segment contains characters outside the
            safe identifier alphabet.
        """
        parts = field.split(".")
        top = parts[0]
        if top not in self._fields:
            raise KeyError(
                f"unknown field {top!r} on {self._model_cls.__name__}; "
                f"known: {sorted(self._fields)}"
            )
        for segment in parts[1:]:
            if not _SAFE_SEGMENT.fullmatch(segment):
                raise ValueError(
                    f"dotted path segment {segment!r} in field {field!r} "
                    "contains characters outside [A-Za-z_][A-Za-z0-9_]*; "
                    "this is rejected to prevent JSONB-key injection"
                )

    # ------------------------------------------------------------------
    # Builder methods — all return self for chaining
    # ------------------------------------------------------------------

    def where(self, field: str, value: Any, op: Op = Op.EQ) -> "Q[ModelT]":
        """Add a comparison predicate.

        Parameters
        ----------
        field:
            Model field name (validated).
        value:
            Literal value to compare against.
        op:
            Comparison operator; defaults to ``Op.EQ``.
        """
        self._check_field(field)
        self._predicates.append(
            Predicate(left=FieldRef(name=field), op=op, right=Value(value=value))
        )
        return self

    def where_in(self, field: str, values: Sequence[Any]) -> "Q[ModelT]":
        """Add an ``IN`` predicate (field value is one of *values*)."""
        self._check_field(field)
        self._predicates.append(
            Predicate(
                left=FieldRef(name=field),
                op=Op.IN,
                right=Value(value=list(values)),
            )
        )
        return self

    def where_null(self, field: str) -> "Q[ModelT]":
        """Add an ``IS NULL`` predicate (field equals ``None``)."""
        self._check_field(field)
        self._predicates.append(
            Predicate(left=FieldRef(name=field), op=Op.EQ, right=Value(value=None))
        )
        return self

    def where_not_null(self, field: str) -> "Q[ModelT]":
        """Add an ``IS NOT NULL`` predicate (field is not ``None``)."""
        self._check_field(field)
        self._predicates.append(
            Predicate(left=FieldRef(name=field), op=Op.NE, right=Value(value=None))
        )
        return self

    def where_op(self, field: str, op: Op, value: Any) -> "Q[ModelT]":
        """Add a predicate with an explicit operator.

        Escape hatch for non-equality ops (``LT``, ``GT``, ``LIKE``, …).
        Field validation is identical to :meth:`where`.
        """
        self._check_field(field)
        self._predicates.append(
            Predicate(left=FieldRef(name=field), op=op, right=Value(value=value))
        )
        return self

    # ------------------------------------------------------------------
    # Combinators
    # ------------------------------------------------------------------

    @classmethod
    def or_(cls, *qs: "Q[ModelT]") -> "Q[ModelT]":
        """Combine multiple ``Q`` instances with logical OR.

        All supplied ``Q`` instances must share the same ``model_cls``;
        a mismatch raises ``TypeError``.  At least two ``Q`` instances
        must be supplied.

        The combined predicates are assembled left-to-right into a
        binary OR tree::

            Q.or_(q1, q2, q3)
            # → OR(OR(build(q1), build(q2)), build(q3))
        """
        if len(qs) < 2:
            raise ValueError("Q.or_() requires at least two Q instances")
        model_cls = qs[0]._model_cls
        for q in qs[1:]:
            if q._model_cls is not model_cls:
                raise TypeError(
                    f"Q.or_() received Q instances with different model classes: "
                    f"{model_cls.__name__!r} vs {q._model_cls.__name__!r}"
                )
        # Build a synthetic Q that holds the combined OR predicate.
        combined: Q[ModelT] = cls.__new__(cls)
        combined._model_cls = model_cls
        combined._fields = frozenset(model_cls.model_fields)
        # Fold left-to-right into an OR tree.
        result: Predicate = qs[0].build()
        for q in qs[1:]:
            result = Predicate(left=result, op=Op.OR, right=q.build())
        combined._predicates = [result]
        return combined

    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------

    def build(self) -> Predicate:
        """Compile accumulated predicates into a single :class:`Predicate`.

        Returns
        -------
        Predicate
            A single predicate (if only one ``where`` call) or a
            left-leaning AND tree (for multiple ``where`` calls).

        Raises
        ------
        ValueError
            When no ``where`` calls have been made.
        """
        if not self._predicates:
            raise ValueError(
                "Q.build() called on an empty Q; "
                "add at least one where() / where_in() / where_null() / where_not_null() call"
            )
        if len(self._predicates) == 1:
            return self._predicates[0]
        result = self._predicates[0]
        for p in self._predicates[1:]:
            result = Predicate(left=result, op=Op.AND, right=p)
        return result
