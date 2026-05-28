"""CDC hook factory for the internal-collections subsystem.

Wires :class:`make_crud_router`'s ``on_create`` / ``on_update`` /
``on_delete`` callbacks to :meth:`InternalCollectionsSubsystem.enqueue`
so every API mutation of a tracked entity (Agent, Graph, Collection,
Tool) propagates into the vector store between bootstraps. Without
this, the only way to refresh the vector index is to re-run
``POST /v1/internal_collections/bootstrap``.

Safe before the subsystem is activated: when
``request.app.state.internal_collections`` is ``None`` the hooks no-op
silently, so routers can wire them unconditionally.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request

from matrix.internal_collections import EntityType, IngestEvent
from matrix.model.common import Identifiable


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CDC kind registry
# ---------------------------------------------------------------------------

_CDC_KINDS: dict[str, type] = {}


def register_cdc_kind(kind: str, model_cls: type) -> None:
    """Register a CDC entity kind.

    Raises :class:`ValueError` if *kind* is already registered with a
    **different** model class.  Re-registering the same class is idempotent
    (safe on module re-import).
    """
    existing = _CDC_KINDS.get(kind)
    if existing is not None and existing is not model_cls:
        raise ValueError(
            f"CDC kind {kind!r} already registered with {existing!r}"
        )
    _CDC_KINDS[kind] = model_cls


def known_cdc_kinds() -> dict[str, type]:
    """Return a copy of the registered CDC kinds."""
    return dict(_CDC_KINDS)


def _reset_for_test() -> None:
    """Test-only helper — clear the registry between test cases."""
    _CDC_KINDS.clear()


# ---------------------------------------------------------------------------

_OnMutateHook = Callable[[str, Request], Awaitable[None]]


def make_cdc_hooks(
    entity_type: EntityType,
    model_cls: type[Identifiable],
) -> tuple[_OnMutateHook, _OnMutateHook, _OnMutateHook]:
    """Return ``(on_create, on_update, on_delete)`` hooks that enqueue
    one CDC event per mutation against the named ``entity_type``.

    Hooks no-op when the :class:`InternalCollectionsSubsystem` is not
    attached to the app state, so a deployment that has not bootstrapped
    the subsystem still gets normal CRUD behaviour.
    """

    async def _upsert(entity_id: str, request: Request) -> None:
        ic = getattr(request.app.state, "internal_collections", None)
        if ic is None:
            return
        sp = getattr(request.app.state, "storage_provider", None)
        if sp is None:
            return
        storage = sp.get_storage(model_cls)
        entity = await storage.get(entity_id)
        if entity is None:
            # Mutation hook fired but the row vanished between commit and
            # this read -- treat as a delete to keep the index consistent.
            ic.enqueue(IngestEvent(
                op="delete",
                entity_type=entity_type,
                entity_id=entity_id,
            ))
            return
        ic.enqueue(IngestEvent(
            op="upsert",
            entity_type=entity_type,
            entity_id=entity_id,
            payload=entity.model_dump(mode="json"),
        ))

    async def _delete(entity_id: str, request: Request) -> None:
        ic = getattr(request.app.state, "internal_collections", None)
        if ic is None:
            return
        ic.enqueue(IngestEvent(
            op="delete",
            entity_type=entity_type,
            entity_id=entity_id,
        ))

    _upsert.__name__ = f"_cdc_upsert_{entity_type}"
    _delete.__name__ = f"_cdc_delete_{entity_type}"
    return _upsert, _upsert, _delete


__all__ = [
    "make_cdc_hooks",
    "register_cdc_kind",
    "known_cdc_kinds",
    "_reset_for_test",
]
