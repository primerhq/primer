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


__all__ = [
    "Describeable",
    "Identifiable",
    "dump_for_storage",
]
