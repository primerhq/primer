"""A pure-Python statement of ``Storage.patch_if`` semantics.

The oracle the in-memory test fake implements ``patch_if`` with. The real backends are held to the
same behaviour by running ``tests/storage/_patch_scenarios.py`` against SQLite, Postgres AND the fake,
so a fake that drifts from the backends fails the contract instead of lying to every test that uses it.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from primer.storage._patch import PatchSpecError, check_canonical_json, parent_paths


def typed_equal(a: Any, b: Any) -> bool:
    """JSON-typed equality: ``True`` is not ``1`` and not ``"true"``; ``1`` equals ``1.0``; containers element by element."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(typed_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(typed_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def same_spelling(a: Any, b: Any) -> bool:
    """Like :func:`typed_equal` but ``5`` is not ``5.0``: whether two stored values are the SAME JSON text."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_spelling(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_spelling(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def value_at(doc: Mapping[str, Any], field: str | tuple[str, ...]) -> Any:
    """What a ``where`` key names: a top-level field, or the leaf a path names (None when absent, under an absent or non-object parent too)."""
    if not isinstance(field, tuple):
        return doc.get(field)
    node: Any = doc
    for part in field:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def doc_matches(doc: Mapping[str, Any], where: Mapping[Any, Sequence[Any]]) -> bool:
    """Every key must match (AND); any listed value matches (OR); an absent field or leaf reads as None."""
    return all(
        any(typed_equal(value_at(doc, field), v) for v in allowed)
        for field, allowed in where.items()
    )


def apply_patch(
    doc: Mapping[str, Any],
    patch: Mapping[str, Any],
    set_paths: Mapping[tuple[str, ...], Any],
) -> dict[str, Any]:
    out = copy.deepcopy(dict(doc))
    out.update(copy.deepcopy(dict(patch)))
    for parent in parent_paths(set_paths):
        cur = out
        for part in parent[:-1]:
            cur = cur[part]
        if not isinstance(cur.get(parent[-1]), dict):
            cur[parent[-1]] = {}
    for path in sorted(set_paths):
        cur = out
        for part in path[:-1]:
            cur = cur[part]
        cur[path[-1]] = copy.deepcopy(set_paths[path])
    return out


def _nullable(annotation: Any) -> bool:
    """May this annotation hold None? Unknown forms count as nullable (the safe direction)."""
    import types
    import typing

    if annotation is None or annotation is Any or annotation is type(None) or annotation is object:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _nullable(typing.get_args(annotation)[0])
    if origin is typing.Union or origin is types.UnionType:
        return any(_nullable(a) for a in typing.get_args(annotation))
    if origin is typing.Literal:
        return None in typing.get_args(annotation)
    if origin is not None:
        return not isinstance(origin, type)
    return not isinstance(annotation, type)


def where_with_defaults(model_cls: Any, where: Mapping[str, Sequence[Any]]) -> dict[str, list[Any]]:
    """A guard that names the DEFAULT of a field that cannot hold null also matches the field being ABSENT (how the
    model reads a row older than the field). A nullable field, a factory default and a secret are left alone."""
    from pydantic import SecretStr
    from pydantic_core import to_jsonable_python

    out: dict[str, list[Any]] = {}
    for field, allowed in where.items():
        values = list(allowed)
        info = model_cls.model_fields.get(field)
        if (
            info is not None and not info.is_required() and info.default_factory is None
            and not _nullable(info.annotation) and not isinstance(info.default, SecretStr)
            and not any(v is None for v in values)
        ):
            default = to_jsonable_python(info.default)
            if any(typed_equal(v, default) for v in values):
                values.append(None)
        out[field] = values
    return out


def check_known_fields_reference(
    model_cls: Any, patch: Mapping[str, Any], set_paths: Mapping[tuple[str, ...], Any],
) -> None:
    if model_cls.model_config.get("extra") != "allow":
        unknown = sorted({*patch, *(p[0] for p in set_paths)} - set(model_cls.model_fields))
        if unknown:
            raise PatchSpecError(f"{model_cls.__name__} has no field {unknown[0]!r}")


def patch_if_reference(
    model_cls: Any,
    id: str,  # noqa: A002
    raw_doc: Mapping[str, Any],
    patch: Mapping[str, Any] | None,
    *,
    where: Mapping[str, Sequence[Any]],
    set_paths: Mapping[tuple[str, ...], Any] | None = None,
) -> tuple[dict[str, Any], Any] | None:
    """The whole ``patch_if`` pipeline over the RAW stored document: ``None`` when ``where`` rejects it, else
    ``(new stored document, validated model)``.

    Mirrors what the backends do, in plain Python: validate the spec, refuse unknown fields, guard against the raw
    document (with defaulted-absent fields matching), apply the patch to the raw document (so keys the model does
    not read survive), validate the result, refuse a set_paths leaf the model does not carry, and rewrite every
    field the patch touched to the model's canonical dump (refusing one that strict JSON cannot hold).
    """
    from primer.model.common import dump_for_storage
    from primer.storage._patch import validate_patch

    patch_d, paths_d, where_d = validate_patch(patch, set_paths, where)
    check_known_fields_reference(model_cls, patch_d, paths_d)
    if not doc_matches(raw_doc, where_with_defaults(model_cls, where_d)):
        return None
    produced = apply_patch(raw_doc, patch_d, paths_d)
    entity = model_cls.model_validate({**produced, "id": id})
    canonical = {k: v for k, v in dump_for_storage(entity).items() if k != "id"}
    for path in paths_d:
        node: Any = canonical
        for part in path:
            if not isinstance(node, dict) or part not in node:
                raise PatchSpecError(f"set_paths {path!r} is not part of {model_cls.__name__}")
            node = node[part]
    for key in sorted({*patch_d, *(p[0] for p in paths_d)}):   # sorted: the field a refusal names must not depend on the hash seed
        if key in canonical and not same_spelling(produced.get(key), canonical[key]):
            check_canonical_json(model_cls.__name__, key, canonical[key])   # a "nan" the model made a float is refused
            produced[key] = canonical[key]
    return produced, entity


def patch_model(
    current: Any,
    patch: Mapping[str, Any] | None,
    *,
    where: Mapping[str, Sequence[Any]],
    set_paths: Mapping[tuple[str, ...], Any] | None = None,
) -> Any | None:
    """The model ``current`` becomes under ``patch_if``, or ``None`` when ``where`` rejects it.

    For test storages that hold models and need their own visibility rules around the write (a per-connection
    view, say) but not their own copy of the patch semantics: the stored document is the model's own dump, and the
    rest is :func:`patch_if_reference`.
    """
    from primer.model.common import dump_for_storage

    doc = {k: v for k, v in dump_for_storage(current).items() if k != "id"}   # the id is a column
    out = patch_if_reference(type(current), current.id, doc, patch, where=where, set_paths=set_paths)
    return None if out is None else out[1]
