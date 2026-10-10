"""Which fields of a stored model hold a secret, so that no query may use them (ticket 01a1212a).

A row is stored in its STORAGE form (:func:`primer.model.common.dump_for_storage`) and served in its JSON form. The two differ for exactly two
kinds of value:

* a pydantic secret (``SecretStr``, ``SecretBytes``, ``Secret[...]``): the read serves it masked and the storage dump puts the plaintext back;
* a value whose serializer reads the dump's context (the masked URLs: the storage dump passes
  :data:`~primer.model.common.STORAGE_DUMP_CONTEXT` and gets the real URL back, the read gets the password masked). A serializer that cannot
  see the context (it takes no ``info`` argument, like the base64 one of a ``bytes`` field) dumps the same JSON for both, so its value is
  served exactly as it is stored.

A predicate, a sort key and a cursor's seek key are compared with the STORED document, so on such a field they answer questions about the value
the read masks: ``LIKE 'sk-a%'`` is a prefix oracle, a sort or a ``>=`` a binary search. A field "holds a secret" when such a value is anywhere in
its type (a list of models whose url is masked, a ``dict[str, SecretStr]``, a config union one member of which has an api key): the whole field
is compared as JSON text, the secret included. Every other path is served exactly as it is stored, so a query on it reveals nothing a reader
cannot read, which is also why a cursor may carry the served value of every key it is allowed to have (``_encode_cursor_for``).

:func:`refuse_secret_fields` is the one check: the REST list/find routes and the list/find tools call it before they query, and both storage
backends call :func:`refuse_secret_path` from the renderer that turns a field path into SQL, so nothing reaches the stored document past it.
"""

from __future__ import annotations

import collections.abc
import dataclasses
import functools
import inspect
import types
import typing
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, PlainSerializer, Secret, SecretBytes, SecretStr, WrapSerializer

from primer.model.except_ import ValidationError
from primer.model.storage import FieldRef, OrderBy, Predicate


_SECRET_TYPES = (SecretStr, SecretBytes, Secret)

# Marks a model field that has a ``@field_serializer``: it may read the context, so it is treated as one that does.
_FIELD_SERIALIZER = object()


def _takes_info(func: Any, value_args: int) -> bool:
    """Whether pydantic passes ``func`` the :class:`~pydantic.SerializationInfo` (and so the dump's context).

    The rule pydantic applies to an annotated serializer: count the positional parameters that have no default (the first, the value, may have
    one); one more than the value arguments (1 for a plain serializer, 2 for a wrap serializer, which also takes the handler) is the info.
    """
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return False      # a builtin: pydantic passes it the value only
    positional = [
        p for i, p in enumerate(params)
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and (i == 0 or p.default is p.empty)
    ]
    return len(positional) > value_args


def _masks(meta: Any) -> bool:
    if meta is _FIELD_SERIALIZER:
        return True
    if isinstance(meta, PlainSerializer):
        return _takes_info(meta.func, 1)
    if isinstance(meta, WrapSerializer):
        return _takes_info(meta.func, 2)
    return False


def _alternatives(tp: Any, meta: tuple[Any, ...] = ()) -> Iterator[tuple[Any, tuple[Any, ...]]]:
    """``(bare type, metadata)`` for each alternative of ``tp``: ``Annotated`` unwrapped (its metadata kept), unions and ``Optional`` split."""
    if isinstance(tp, typing.TypeAliasType):
        yield from _alternatives(tp.__value__, meta)
        return
    origin = get_origin(tp)
    if origin is Annotated:
        base, *more = get_args(tp)
        yield from _alternatives(base, meta + tuple(more))
    elif origin is Union or origin is types.UnionType:
        for arg in get_args(tp):
            yield from _alternatives(arg, meta)
    else:
        yield tp, meta


def _is_masked(bare: Any, meta: tuple[Any, ...]) -> bool:
    if any(_masks(m) for m in meta):
        return True
    cls = get_origin(bare) or bare          # ``Secret[int]`` is an alias of ``Secret``
    if isinstance(cls, type) and issubclass(cls, _SECRET_TYPES):
        return True
    # A model that serializes itself can do anything with the context.
    return isinstance(cls, type) and issubclass(cls, BaseModel) and bool(cls.__pydantic_decorators__.model_serializers)


def _field_node(model_cls: type[BaseModel], name: str) -> tuple[Any, tuple[Any, ...]]:
    """A model field as ``(annotation, metadata)``: pydantic moves a top-level ``Annotated``'s metadata onto the field, so read it there."""
    field = model_cls.model_fields[name]
    meta = tuple(field.metadata)
    serialized = {f for d in model_cls.__pydantic_decorators__.field_serializers.values() for f in d.info.fields}
    if name in serialized or "*" in serialized:
        meta += (_FIELD_SERIALIZER,)
    return field.annotation, meta


def _typed_members(bare: type) -> Iterator[Any] | None:
    """The member types of a dataclass or a TypedDict (pydantic serializes both field by field), or ``None`` for any other class."""
    if not (dataclasses.is_dataclass(bare) or typing.is_typeddict(bare)):
        return None
    return iter(typing.get_type_hints(bare, include_extras=True).values())


def _holds_secret(tp: Any, meta: tuple[Any, ...], seen: set[Any]) -> bool:
    """Whether a value of type ``tp`` can hold a secret anywhere inside it (a depth-first walk; ``seen`` cuts model cycles)."""
    for bare, m in _alternatives(tp, meta):
        if _is_masked(bare, m):
            return True
        if isinstance(bare, (str, typing.ForwardRef)):
            return True    # a type pydantic could not resolve: assume the worst rather than let a secret through
        if isinstance(bare, type) and issubclass(bare, BaseModel):
            if bare not in seen:
                seen.add(bare)
                if any(_holds_secret(*_field_node(bare, name), seen) for name in bare.model_fields):
                    return True
            continue
        if isinstance(bare, type):
            try:
                members = _typed_members(bare)
            except Exception:  # noqa: BLE001 - unresolvable hints: assume the worst
                return True
            if members is not None and bare not in seen:
                seen.add(bare)
                if any(_holds_secret(t, (), seen) for t in members):
                    return True
            continue
        if get_origin(bare) is Literal:
            continue
        # list[X], dict[K, V], tuple[...], set[X] and the like: anything inside them.
        if any(_holds_secret(arg, (), seen) for arg in get_args(bare) if arg is not Ellipsis):
            return True
    return False


def _children(tp: Any, meta: tuple[Any, ...], part: str) -> list[tuple[Any, tuple[Any, ...]]]:
    """What one more path segment ``part`` names below a value of type ``tp``: a model's field of that name, a mapping's value type."""
    out: list[tuple[Any, tuple[Any, ...]]] = []
    for bare, _m in _alternatives(tp, meta):
        if isinstance(bare, type) and issubclass(bare, BaseModel):
            if part in bare.model_fields:
                out.append(_field_node(bare, part))
            continue
        origin = get_origin(bare) or bare
        if isinstance(origin, type) and issubclass(origin, collections.abc.Mapping):
            args = get_args(bare)
            out.append((args[1] if len(args) == 2 else Any, ()))
    return out


@functools.lru_cache(maxsize=4096)
def holds_secret(model_cls: type[BaseModel], path: str) -> bool:
    """Whether the (possibly dotted) field ``path`` of ``model_cls`` holds a value the read masks: the value at that path, anything inside it, or
    anything the path passes through.

    A segment the type does not declare (a key no member of a union has, a list index, JSON-path syntax) stops the walk, and the path is judged by
    everything below the node it reached: the backend would read SOMETHING inside that node (SQLite's ``$.a.b[0]`` indexes an array), so a
    node that holds a secret anywhere refuses every path into it that its type cannot follow. A ``path`` whose first segment is not a field of
    the model is left to the renderer, which refuses it as undeclared.
    """
    parts = path.split(".")
    if parts[0] not in model_cls.model_fields:
        return False
    nodes = [_field_node(model_cls, parts[0])]
    for part in parts[1:]:
        if any(_is_masked(bare, m) for tp, meta in nodes for bare, m in _alternatives(tp, meta)):
            return True
        children = [child for tp, meta in nodes for child in _children(tp, meta, part)]
        if not children:
            break
        nodes = children
    return any(_holds_secret(tp, meta, set()) for tp, meta in nodes)


def _secret_error(model_cls: type[BaseModel], path: str) -> ValidationError:
    return ValidationError(
        f"{path!r} holds a secret of {model_cls.__name__} (served masked, stored in clear): it cannot be used in a predicate, "
        "an order_by or a cursor",
    )


def refuse_secret_path(model_cls: type[BaseModel], path: str) -> None:
    """Raise :class:`~primer.model.except_.ValidationError` (a 422) when ``path`` holds a secret (:func:`holds_secret`)."""
    if holds_secret(model_cls, path):
        raise _secret_error(model_cls, path)


def _field_names(predicate: Predicate) -> Iterator[str]:
    for side in (predicate.left, predicate.right):
        if isinstance(side, Predicate):
            yield from _field_names(side)
        elif isinstance(side, FieldRef):
            yield side.name


def refuse_secret_fields(
    model_cls: type[BaseModel],
    *,
    predicate: Predicate | None = None,
    order_by: list[OrderBy] | None = None,
) -> None:
    """Refuse a query that would compare a secret (:func:`holds_secret`): a field of ``predicate`` (either side of any node) or an ``order_by``
    key. Raises :class:`~primer.model.except_.ValidationError`, which the REST error map answers 422 ``validation-error`` and the tools report as
    ``type=validation-error``; the message names the field, never a value."""
    paths = list(_field_names(predicate)) if predicate is not None else []
    paths += [ob.field for ob in order_by or []]
    for path in paths:
        refuse_secret_path(model_cls, path)


__all__ = ["holds_secret", "refuse_secret_fields", "refuse_secret_path"]
