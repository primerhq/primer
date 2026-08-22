"""Registry of entity kinds that emit platform events.

Formerly the CDC kind registry in ``primer.api.routers._cdc_hooks``
(which now re-exports these under their old names). Registration
happens at router-factory call time, so by the time the app serves a
request every kind is known. The storage layer consults
:func:`kind_for_model` on every write: a registered kind's
create/update/delete each append a ``<kind>.created`` /
``<kind>.updated`` / ``<kind>.deleted`` event in the same transaction
as the row write.
"""

from __future__ import annotations

_EVENT_KINDS: dict[str, type] = {}
_KIND_BY_MODEL: dict[type, str] = {}


def register_event_kind(kind: str, model_cls: type) -> None:
    """Register an entity kind for CRUD event emission (and CDC).

    Raises :class:`ValueError` if *kind* is already registered with a
    **different** model class. Re-registering the same class is
    idempotent (safe on module re-import).
    """
    existing = _EVENT_KINDS.get(kind)
    if existing is not None and existing is not model_cls:
        raise ValueError(
            f"event kind {kind!r} already registered with {existing!r}"
        )
    _EVENT_KINDS[kind] = model_cls
    _KIND_BY_MODEL[model_cls] = kind


def known_event_kinds() -> dict[str, type]:
    """Return a copy of the registered kinds."""
    return dict(_EVENT_KINDS)


def kind_for_model(model_cls: type) -> str | None:
    """Reverse lookup: the registered kind for ``model_cls``, or None."""
    return _KIND_BY_MODEL.get(model_cls)


def _reset_for_test() -> None:
    """Test-only helper: clear the registry between test cases."""
    _EVENT_KINDS.clear()
    _KIND_BY_MODEL.clear()


__all__ = [
    "register_event_kind",
    "known_event_kinds",
    "kind_for_model",
    "_reset_for_test",
]
