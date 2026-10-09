"""Shared base models + serialization helpers reused across the schema."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

from pydantic import AnyUrl, BaseModel, Field, SecretStr, model_validator
from pydantic import ValidationError as PydanticValidationError

from primer.common.origin import origin_of
from primer.common.url_userinfo import MASK, MaskCannotBeRestored, carries_mask, restore_userinfo
from primer.model.except_ import ValidationError


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


# The ``context`` of the dump that writes a row to storage (and of anything that needs the row's IDENTITY): a field type that masks something in the JSON-mode dump
# (a provider ``url``'s password, :class:`primer.model.providers._shared.MaskedUserinfoUrl`) returns the real value when its serializer sees this key.
STORAGE_DUMP_CONTEXT: dict[str, Any] = {"storage": True}


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

    A URL field that masks the password of its userinfo in the JSON-mode dump
    (:class:`~primer.model.providers._shared.MaskedUserinfoUrl`) is dumped with
    :data:`STORAGE_DUMP_CONTEXT`, which that serializer reads as "the real URL":
    one mechanism, no second kind of leaf for the walk below.

    Callers in API/router/serialization paths must NOT use this helper —
    they want the redacted default. Anything that FINGERPRINTS or COMPARES a
    row (cache key, reload detection) must use this form too: the served
    form is the same for two URLs that differ only by password.
    """
    dumped = entity.model_dump(mode="json", context=STORAGE_DUMP_CONTEXT)
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


# The fields that name WHERE the credentials kept in the same model are sent. A secret restored from the stored row goes to the origin it was stored for and to no other (ticket 01a1212a).
_ORIGIN_URL_FIELDS = ("url", "base_url", "endpoint_url", "apiserver_url", "discovery_url", "git_url", "resource_uri")

REENTER_KEY = "re-enter the key: the stored one is kept only for the same origin (scheme, host and port)"
_NOT_A_SECRET = "this is the mask a GET serves, not a secret: re-enter the key"


def _origin_of_model(model: BaseModel) -> tuple[Any, ...] | None:
    """Where the credentials of ``model`` are sent: the origin (scheme, host, port; :func:`primer.common.origin.origin_of`) of each of its URL fields, and for a ``hostname`` field the host
    with the ``port``. ``None`` when the model names no origin at all (a Hugging Face token, a web-search key): there is nothing to bind to."""
    fields = model.__class__.model_fields
    parts: list[Any] = []
    for name in _ORIGIN_URL_FIELDS:
        if name in fields:
            value = getattr(model, name, None)
            parts.append((name, None if value is None else origin_of(str(value))))
    if "hostname" in fields:
        host = getattr(model, "hostname", None)
        parts.append(("hostname", None if host is None else (str(host).strip().lower(), getattr(model, "port", None))))
    return tuple(parts) or None


def _moved_origin(entity: BaseModel, existing: BaseModel, moved: bool) -> bool:
    """True when ``entity`` or a model it sits in names another origin than the stored one: an endpoint that appeared or went away counts as another origin."""
    if moved:
        return True
    origin = _origin_of_model(entity)
    return origin is not None and origin != _origin_of_model(existing)


def _restore_secret(name: str, new_value: SecretStr, old_value: Any, moved: bool) -> SecretStr | None:
    """The stored secret when ``new_value`` is the mask a GET served for it, else ``None`` (the secret is left as the person sent it).

    A served mask under a MOVED origin is REFUSED with a 422, naming no secret: restoring it would send the stored credential to whatever host the update names.
    """
    if not isinstance(old_value, SecretStr) or not _matches_served_mask(new_value.get_secret_value(), old_value.get_secret_value()):
        return None
    if moved:
        raise ValidationError(f"{name}: {REENTER_KEY}")
    return old_value


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

    A restored secret goes only where it was stored for: when the model, or a model it sits in,
    names another origin than the stored one (the scheme, host or port of its ``url`` /
    ``endpoint_url`` / ``apiserver_url`` / ``discovery_url`` / ``git_url`` / ``resource_uri``, or its
    ``hostname`` and ``port``; see :func:`_origin_of_model`), a served mask is REFUSED with a
    :class:`~primer.model.except_.ValidationError` (a 422, ``re-enter the key``) instead of being
    restored: otherwise an update that points the base URL at a host the caller controls and leaves
    the key's mask alone would store the real key next to that host. A secret the person typed is
    theirs and is stored as sent, wherever the URL points; a model with no origin is unaffected.

    Where there is NO stored value of the same shape to restore from (the config class changed,
    a dict key is new, a list changed length) a served mask, a URL's password or a secret, is
    REFUSED too: it would be stored as the value. A field of the same class that never held a
    secret (``existing``'s value is ``None``) is the one case left: an incoming mask-shaped string
    there is stored as a literal secret. This is a known, accepted limitation (nobody's real API
    key IS the string ``"**********"``), not a bug this function tries to close.

    Recurses into nested ``BaseModel`` fields (provider ``config``
    unions), list items, and dict values - covering every ``SecretStr``
    shape actually used in this schema, including ``dict[str,
    SecretStr]`` (toolset ``env`` / ``headers``).
    """
    _preserve(entity, existing, False)


def _preserve(entity: Any, existing: Any, moved: bool) -> None:
    if not isinstance(entity, BaseModel):
        return
    if not isinstance(existing, BaseModel) or entity.__class__ is not existing.__class__:
        _refuse_masks(entity, "", True)          # nothing stored of this shape to restore from (the config class changed): a served mask in it, a password or a key, would be stored as the value
        return
    moved = _moved_origin(entity, existing, moved)
    for name in entity.__class__.model_fields:
        new_value = getattr(entity, name, None)
        old_value = getattr(existing, name, None)
        if isinstance(new_value, SecretStr):
            restored = _restore_secret(name, new_value, old_value, moved)
            if restored is not None:
                setattr(entity, name, restored)
        elif isinstance(new_value, AnyUrl):
            restored_url = _restored_url(name, new_value, old_value)
            if restored_url is not None:
                setattr(entity, name, restored_url)
        elif isinstance(new_value, BaseModel):
            _preserve(new_value, old_value, moved)
        elif isinstance(new_value, list):
            _preserve_list(name, new_value, old_value, moved)
        elif isinstance(new_value, dict):
            _preserve_dict(name, new_value, old_value, moved)


_UNRESTORABLE = "re-enter the password: the stored one is kept only for the same origin (scheme, host and port) and user"


def _restored_url(name: str, new_value: AnyUrl, old_value: Any) -> AnyUrl | None:
    """``new_value`` with the stored URL's credential put back when it is the mask a GET served for ``old_value`` AND the origin and user are the stored ones (see
    :func:`primer.common.url_userinfo.restore_userinfo`), else ``None``: the URL is left as the person sent it.

    A mask that cannot be restored is REFUSED with a 422, whatever the reason (the origin or the user changed, nothing was stored): the stored credential is never given to another host,
    and the literal mask is never stored as the password. A restored URL that is over the length limit although the masked one was not is a 422 too.
    """
    if not isinstance(old_value, AnyUrl):
        if carries_mask(str(new_value)):
            raise ValidationError(f"{name}: {_UNRESTORABLE}")
        return None
    try:
        restored = restore_userinfo(str(new_value), str(old_value))
    except MaskCannotBeRestored as exc:
        raise ValidationError(f"{name}: {_UNRESTORABLE} ({exc})") from None
    if restored is None:
        return None
    try:
        return type(new_value)(restored)
    except PydanticValidationError:
        raise ValidationError(f"{name}: the URL with the stored password put back is too long; re-enter the password") from None


def _looks_served(plain: str) -> bool:
    """True when ``plain`` has the shape of a mask a GET serves: the bare ``"**********"`` or the tail form (the mask and the last four characters, the ``ApiKeySecret`` shape)."""
    return plain == MASK or (len(plain) == len(MASK) + 4 and plain.startswith(MASK))


def _refuse_masks(value: Any, name: str, secrets: bool) -> None:
    """Refuse a URL that carries the served mask anywhere in ``value`` (a model, a list or a dict of them), and with ``secrets`` a secret that is one: there is nothing stored to restore it from."""
    if isinstance(value, SecretStr):
        if secrets and _looks_served(value.get_secret_value()):
            raise ValidationError(f"{name or 'secret'}: {_NOT_A_SECRET}")
    elif isinstance(value, AnyUrl):
        if carries_mask(str(value)):
            raise ValidationError(f"{name or 'url'}: {_UNRESTORABLE}")
    elif isinstance(value, BaseModel):
        for field in value.__class__.model_fields:
            _refuse_masks(getattr(value, field, None), field, secrets)
    elif isinstance(value, list):
        for item in value:
            _refuse_masks(item, name, secrets)
    elif isinstance(value, dict):
        for key, item in value.items():
            _refuse_masks(item, str(key), secrets)


def refuse_served_masks(entity: Any) -> None:
    """Refuse a CREATE body that carries a mask a GET serves, as a URL's password or as a secret (a 422 whose text names no secret).

    A create has nothing stored to restore a mask from, so the literal mask would be stored as the value: the copy-a-provider move (``get_*`` and then ``create_*`` under a new id) stored the
    URL's ``**********`` and the key's ``**********abcd``. The person re-enters the secret.
    """
    _refuse_masks(entity, "", True)


def _preserve_list(name: str, new_items: list[Any], old_items: Any, moved: bool) -> None:
    if not isinstance(old_items, list) or len(old_items) != len(new_items):
        _refuse_masks(new_items, name, True)          # the items cannot be paired with stored ones: a served mask would be stored as the value
        return
    for i, (new_item, old_item) in enumerate(zip(new_items, old_items)):
        if isinstance(new_item, SecretStr):
            restored = _restore_secret(name, new_item, old_item, moved)
            if restored is not None:
                new_items[i] = restored
        elif isinstance(new_item, AnyUrl):
            restored_url = _restored_url(name, new_item, old_item)
            if restored_url is not None:
                new_items[i] = restored_url
        elif isinstance(new_item, BaseModel):
            _preserve(new_item, old_item, moved)
        elif isinstance(new_item, dict):
            _preserve_dict(name, new_item, old_item, moved)
        elif isinstance(new_item, list):
            _preserve_list(name, new_item, old_item, moved)


def _preserve_dict(name: str, new_map: dict[Any, Any], old_map: Any, moved: bool) -> None:
    if not isinstance(old_map, dict):
        _refuse_masks(new_map, name, True)
        return
    for k, new_v in new_map.items():
        if k not in old_map:
            _refuse_masks(new_v, f"{name}.{k}", True)          # a key the stored row does not hold: nothing to restore the mask from
            continue
        old_v = old_map[k]
        label = f"{name}.{k}"
        if isinstance(new_v, SecretStr):
            restored = _restore_secret(label, new_v, old_v, moved)
            if restored is not None:
                new_map[k] = restored
        elif isinstance(new_v, AnyUrl):
            restored_url = _restored_url(label, new_v, old_v)
            if restored_url is not None:
                new_map[k] = restored_url
        elif isinstance(new_v, BaseModel):
            _preserve(new_v, old_v, moved)
        elif isinstance(new_v, list):
            _preserve_list(label, new_v, old_v, moved)
        elif isinstance(new_v, dict):
            _preserve_dict(label, new_v, old_v, moved)


__all__ = [
    "REENTER_KEY",
    "Describeable",
    "Identifiable",
    "dump_for_storage",
    "preserve_masked_secrets",
    "refuse_served_masks",
]
