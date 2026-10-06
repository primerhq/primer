"""The domain error of the pre-write checks the REST routers and the system tools share (task 01a111d1, D5 phase 2b).

The REST routers run per-entity validators before a create or update (``make_crud_router``'s ``on_pre_create`` /
``on_pre_update``). The system toolset's generic ``create_`` / ``update_<entity>`` tools used to store what those validators
refuse. Each validator is now one function over ``(entity, storage_provider)`` that raises :class:`EntityCheckError`; the REST hook
catches it and re-raises EXACTLY the exception it raised before (so the wire body is unchanged by construction), and the tool maps it
to a typed tool error.

This module is deliberately a leaf: it imports nothing from primer, so any layer may raise or catch it.
"""

from __future__ import annotations

from typing import Any, Literal

CheckKind = Literal["conflict", "validation"]


class EntityCheckError(Exception):
    """A pre-write check refused an entity.

    ``kind`` is ``"conflict"`` (the write clashes with a stored row: REST 409, tool ``conflict``) or ``"validation"`` (the body is
    well formed but semantically refused: REST 422, tool ``validation-error``). ``code`` is a stable machine name some REST bodies
    carry, ``field`` the dotted path of the offending field (``approval.policy``), and ``extra`` any additional fields the REST body
    carries (``lineno`` of a python registration error).
    """

    def __init__(
        self,
        kind: CheckKind,
        message: str,
        *,
        code: str | None = None,
        field: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = code
        self.field = field
        self.extra: dict[str, Any] = dict(extra or {})

    def tool_message(self) -> str:
        """The message a tool answers with: the field path first when there is one, so an agent can find what to fix."""
        return f"{self.field}: {self.message}" if self.field else self.message

    @property
    def tool_error_type(self) -> str:
        return "conflict" if self.kind == "conflict" else "validation-error"


__all__ = ["CheckKind", "EntityCheckError"]
