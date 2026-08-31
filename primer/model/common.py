"""Shared base models + serialization helpers reused across the schema."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, Field, SecretStr, model_validator


class Identifiable(BaseModel):
    """Mixin granting a string identifier.

    On create the ``id`` may be omitted: a subclass that sets the
    ``_id_prefix`` ClassVar autogenerates ``<prefix>-<hex>`` (e.g.
    ``agent-3f9a1c8d``); a subclass without a prefix still requires an
    explicit id. After validation ``id`` is always a non-empty string.
    """

    # Subclasses that may autogenerate set this to their id prefix.
    _id_prefix: ClassVar[str | None] = None

    id: str | None = Field(
        default=None,
        description=(
            "Identifier. Optional on create: when omitted, the server "
            "assigns ``<type-prefix>-<hex>`` (e.g. ``agent-3f9a1c8d``). "
            "Immutable after creation."
        ),
    )

    @model_validator(mode="after")
    def _assign_id(self) -> "Identifiable":
        if not self.id:
            prefix = type(self)._id_prefix
            if prefix is None:
                raise ValueError("id is required for this entity type")
            object.__setattr__(self, "id", f"{prefix}-{uuid4().hex[:12]}")
        return self


class Describeable(Identifiable):
    """Mixin adding a free-form human-readable description to an :class:`Identifiable`.

    Use this for configuration entries that are surfaced to humans (e.g. in
    UIs, logs, or help text) and benefit from a short prose explanation
    alongside their machine identifier.
    """

    description: str = Field(
        ...,
        description="Free-form human-readable description of the entry.",
    )


# ===========================================================================
# Serialization helpers
# ===========================================================================


def dump_for_storage(entity: BaseModel) -> dict[str, Any]:
    """JSON-mode model dump that preserves SecretStr plaintext.

    The default :meth:`BaseModel.model_dump` (mode='json') redacts every
    :class:`SecretStr` field to ``'**********'`` — that is the right
    behaviour for API responses but breaks the storage round-trip:
    write -> read of a Provider would return masked credentials and the
    application would fail every subsequent provider call.

    This helper does the same dump and then walks the entity tree,
    replacing each masked placeholder with the live secret value so that
    the JSONB blob written to Postgres carries the real credential.

    Callers in API/router/serialization paths must NOT use this helper —
    they want the redacted default.
    """
    dumped = entity.model_dump(mode="json")
    _unmask_secrets(dumped, entity)
    return dumped


def _unmask_secrets(dumped: Any, entity: Any) -> None:
    """Recursively walk ``entity`` and overwrite masked secrets in
    ``dumped`` with their plaintext values.

    Handles the four containers we encounter in practice: BaseModel
    instances, lists of BaseModel/SecretStr, dicts whose values are
    SecretStr, and dicts whose values are BaseModel.
    """
    if isinstance(entity, BaseModel):
        if not isinstance(dumped, dict):
            return
        for name in entity.__class__.model_fields:
            value = getattr(entity, name, None)
            if value is None or name not in dumped:
                continue
            if isinstance(value, SecretStr):
                dumped[name] = value.get_secret_value()
            elif isinstance(value, BaseModel):
                _unmask_secrets(dumped[name], value)
            elif isinstance(value, list):
                _unmask_list(dumped[name], value)
            elif isinstance(value, dict):
                _unmask_dict(dumped[name], value)


def _unmask_list(dumped: Any, items: list[Any]) -> None:
    if not isinstance(dumped, list) or len(dumped) != len(items):
        return
    for i, item in enumerate(items):
        if isinstance(item, SecretStr):
            dumped[i] = item.get_secret_value()
        elif isinstance(item, BaseModel):
            _unmask_secrets(dumped[i], item)
        elif isinstance(item, dict):
            _unmask_dict(dumped[i], item)
        elif isinstance(item, list):
            _unmask_list(dumped[i], item)


def _unmask_dict(dumped: Any, mapping: dict[Any, Any]) -> None:
    if not isinstance(dumped, dict):
        return
    for k, v in mapping.items():
        if k not in dumped:
            continue
        if isinstance(v, SecretStr):
            dumped[k] = v.get_secret_value()
        elif isinstance(v, BaseModel):
            _unmask_secrets(dumped[k], v)
        elif isinstance(v, list):
            _unmask_list(dumped[k], v)
        elif isinstance(v, dict):
            _unmask_dict(dumped[k], v)


def _matches_served_mask(incoming_plain: str, existing_plain: str) -> bool:
    """True when ``incoming_plain`` is what a GET could have served for
    ``existing_plain``, under EITHER masking convention used across the
    schema: the plain Pydantic default (``"**********"``, used by
    password / access-key / token fields with no custom serializer) or
    the tail-revealing ``ApiKeySecret`` shape
    (:func:`primer.model.providers._shared._mask_with_tail`, used by
    LLM/embedding ``api_key`` fields).

    Checking both shapes independently - rather than picking ONE based
    on ``existing_plain``'s length - matters: a >4-character secret on a
    plain (tail-less) field is served as bare ``"**********"``, not a
    tail-form: computing only the tail-form for that length and
    comparing against it would miss the real, actually-served mask.
    """
    if incoming_plain == "**********":
        return True
    return len(existing_plain) > 4 and incoming_plain == "**********" + existing_plain[-4:]


def preserve_masked_secrets(entity: Any, existing: Any) -> None:
    """Restore secret fields a full-replace PUT never actually changed.

    ``GET`` serves every :class:`SecretStr` field masked (see
    :func:`_matches_served_mask`), and ``PUT`` on a
    :func:`~primer.api.routers._crud.make_crud_router` route is a full
    replace, not a merge. A client that round-trips the served value
    back unchanged - or a UI that blanks the field when the operator
    doesn't intend to touch it - would otherwise persist the literal
    mask string (or an empty secret), corrupting or erasing the real
    credential. Call this on the incoming entity BEFORE
    ``storage.update()`` (an ``on_pre_update`` hook, mutating ``entity``
    in place): any ``SecretStr`` field whose incoming plaintext equals
    what would have been served for ``existing``'s CURRENT value is
    swapped back for that real value.

    A field that never held a secret (``existing``'s value is ``None``)
    has nothing to restore - an incoming mask-shaped string in that case
    is stored as a literal secret. This is a known, accepted limitation
    (nobody's real API key IS the string ``"**********"``), not a bug
    this function tries to close.

    Recurses into nested ``BaseModel`` fields (provider ``config``
    unions), list items, and dict values - covering every ``SecretStr``
    shape actually used in this schema, including ``dict[str,
    SecretStr]`` (toolset ``env`` / ``headers``).
    """
    if not isinstance(entity, BaseModel) or not isinstance(existing, BaseModel):
        return
    if entity.__class__ is not existing.__class__:
        return
    for name in entity.__class__.model_fields:
        new_value = getattr(entity, name, None)
        old_value = getattr(existing, name, None)
        if isinstance(new_value, SecretStr):
            if isinstance(old_value, SecretStr) and _matches_served_mask(
                new_value.get_secret_value(), old_value.get_secret_value(),
            ):
                setattr(entity, name, old_value)
        elif isinstance(new_value, BaseModel):
            preserve_masked_secrets(new_value, old_value)
        elif isinstance(new_value, list):
            _preserve_masked_secrets_list(new_value, old_value)
        elif isinstance(new_value, dict):
            _preserve_masked_secrets_dict(new_value, old_value)


def _preserve_masked_secrets_list(new_items: list[Any], old_items: Any) -> None:
    if not isinstance(old_items, list) or len(old_items) != len(new_items):
        return
    for i, (new_item, old_item) in enumerate(zip(new_items, old_items)):
        if isinstance(new_item, SecretStr):
            if isinstance(old_item, SecretStr) and _matches_served_mask(
                new_item.get_secret_value(), old_item.get_secret_value(),
            ):
                new_items[i] = old_item
        elif isinstance(new_item, BaseModel):
            preserve_masked_secrets(new_item, old_item)
        elif isinstance(new_item, dict):
            _preserve_masked_secrets_dict(new_item, old_item)
        elif isinstance(new_item, list):
            _preserve_masked_secrets_list(new_item, old_item)


def _preserve_masked_secrets_dict(new_map: dict[Any, Any], old_map: Any) -> None:
    if not isinstance(old_map, dict):
        return
    for k, new_v in new_map.items():
        if k not in old_map:
            continue
        old_v = old_map[k]
        if isinstance(new_v, SecretStr):
            if isinstance(old_v, SecretStr) and _matches_served_mask(
                new_v.get_secret_value(), old_v.get_secret_value(),
            ):
                new_map[k] = old_v
        elif isinstance(new_v, BaseModel):
            preserve_masked_secrets(new_v, old_v)
        elif isinstance(new_v, list):
            _preserve_masked_secrets_list(new_v, old_v)
        elif isinstance(new_v, dict):
            _preserve_masked_secrets_dict(new_v, old_v)


__all__ = [
    "Describeable",
    "Identifiable",
    "dump_for_storage",
]
