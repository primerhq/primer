"""Declarative reference-integrity blocks for DELETE operations.

``ReferenceCheck`` declares that a parent entity must not be deleted while
child records still reference it.  ``build_reference_block_hook`` composes a
list of checks into a single pre-delete async hook suitable for use as
``on_pre_delete`` in :func:`primer.api.routers._crud.make_crud_router`.

Example::

    from primer.api.routers._references import ReferenceCheck, build_reference_block_hook

    channel_provider_router = make_crud_router(
        ...,
        on_pre_delete=build_reference_block_hook([
            ReferenceCheck(
                child_kind="channel",
                child_storage=get_channel_storage,
                child_field="provider_id",
            ),
        ]),
    )
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from primer.model.except_ import ConflictError
from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.model.storage import OffsetPage


@dataclass(frozen=True)
class ReferenceCheck:
    """Declarative child-reference check for a pre-delete hook.

    Parameters
    ----------
    child_kind:
        Human-readable name for the child entity type (used in the 409 payload
        ``child_kind`` field so the client can surface a meaningful error).
    child_storage:
        A callable that accepts a ``Request`` and returns a storage object with
        an async ``find(predicate, page)`` method — typically a FastAPI
        dependency function (e.g. ``get_channel_storage``).
    child_field:
        The foreign-key field on the child model that references the parent
        entity's ``id``.  Set by the router author (never by user input) so it
        is trusted as a valid field name.
    op:
        The :class:`~primer.model.storage.Op` used to match ``child_field``
        against the parent id. Defaults to ``Op.EQ`` (a plain scalar
        foreign-key field). Pass ``Op.CONTAINS`` when ``child_field`` names
        a JSON array field instead (e.g. an ordered ``members: list[str]``)
        -- matches when the array contains the parent id, which is also
        what makes a *self-referential* check (``child_storage`` returning
        the SAME entity's own storage) meaningful: "is this row named
        inside some other row's list field."
    error_code:
        The string placed in the ``error`` key of the 409 response body.
        Defaults to ``"in_use_by"``.
    """

    child_kind: str
    child_storage: Callable[[Request], Any]
    child_field: str
    op: Op = field(default=Op.EQ)
    error_code: str = field(default="in_use_by")


def build_reference_block_hook(
    checks: Sequence[ReferenceCheck],
) -> Callable[[Any, Request], Any]:
    """Return an async pre-delete hook that enforces all *checks* in order.

    The returned function has the pre-delete-entity hook signature
    ``async (entity, request) -> None`` as expected by ``on_pre_delete`` in
    :func:`make_crud_router`.

    For each check the hook calls ``storage.find(predicate, page)`` where
    *predicate* matches ``child_field == entity.id`` and *page* requests at
    most one result.  If any check finds a matching child record the hook
    raises :class:`~primer.model.except_.ConflictError`, which the RFC7807
    error handler (:mod:`primer.api.errors`) turns into a 409 problem
    response whose ``detail`` is the message below (``error_code`` is the
    message's lead, not a separate JSON key)::

        "in_use_by: 1 <child_kind>(s) reference '<parent id>' (first: '<child id>')"

    The item count reflects only the items returned in the single-item page
    (0 or 1); callers should treat it as "at least one child exists".

    Parameters
    ----------
    checks:
        Ordered sequence of :class:`ReferenceCheck` instances to evaluate.
        Evaluation stops at the first check that finds a child.
    """

    async def _hook(entity: Any, request: Request) -> None:
        entity_id: str = entity.id
        for check in checks:
            storage = check.child_storage(request)
            predicate = Predicate(
                left=FieldRef(name=check.child_field),
                op=check.op,
                right=Value(value=entity_id),
            )
            page = await storage.find(predicate, OffsetPage(offset=0, length=1))
            if page.items:
                # RFC7807 conflict envelope (consistent with every other
                # error surface). The detail names the blocking child kind,
                # count, and the first referencing id so the message is
                # actionable; the error_code is carried as the message lead.
                raise ConflictError(
                    f"{check.error_code}: {len(page.items)} "
                    f"{check.child_kind}(s) reference {entity_id!r} "
                    f"(first: {page.items[0].id!r})"
                )

    return _hook


__all__ = ["ReferenceCheck", "build_reference_block_hook"]
