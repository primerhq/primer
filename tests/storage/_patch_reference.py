"""A pure-Python statement of ``Storage.patch_if`` semantics.

The oracle the in-memory test fake implements ``patch_if`` with. The real backends are held to the
same behaviour by running ``tests/storage/_patch_scenarios.py`` against SQLite, Postgres AND the fake,
so a fake that drifts from the backends fails the contract instead of lying to every test that uses it.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from primer.storage._patch import parent_paths


def typed_equal(a: Any, b: Any) -> bool:
    """JSON-typed equality: ``True`` is not ``1`` and not ``"true"``; ``1`` equals ``1.0``."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def doc_matches(doc: Mapping[str, Any], where: Mapping[str, Sequence[Any]]) -> bool:
    """Every field must match (AND); any listed value matches (OR); an absent field reads as None."""
    return all(
        any(typed_equal(doc.get(field), v) for v in allowed)
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


def where_with_defaults(model_cls: Any, where: Mapping[str, Sequence[Any]]) -> dict[str, list[Any]]:
    """A guard that names a field's default also matches the field being ABSENT (how the model reads it)."""
    from pydantic_core import to_jsonable_python

    out: dict[str, list[Any]] = {}
    for field, allowed in where.items():
        values = list(allowed)
        info = model_cls.model_fields.get(field)
        if info is not None and not info.is_required() and not any(v is None for v in values):
            try:
                default = to_jsonable_python(info.get_default(call_default_factory=True))
            except (TypeError, ValueError):
                default = None
            if default is not None and any(typed_equal(v, default) for v in values):
                values.append(None)
        out[field] = values
    return out


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
    not read survive), validate the result, and rewrite every field the patch touched to the model's canonical dump.
    """
    from primer.model.common import dump_for_storage
    from primer.storage._patch import validate_patch

    patch_d, paths_d, where_d = validate_patch(patch, set_paths, where)
    if model_cls.model_config.get("extra") != "allow":
        unknown = sorted({*patch_d, *(p[0] for p in paths_d)} - set(model_cls.model_fields))
        if unknown:
            raise ValueError(f"{model_cls.__name__} has no field {unknown[0]!r}")
    if not doc_matches(raw_doc, where_with_defaults(model_cls, where_d)):
        return None
    produced = apply_patch(raw_doc, patch_d, paths_d)
    entity = model_cls.model_validate({**produced, "id": id})
    canonical = {k: v for k, v in dump_for_storage(entity).items() if k != "id"}
    for key in {*patch_d, *(p[0] for p in paths_d)}:
        if key in canonical and not typed_equal(produced.get(key), canonical[key]):
            produced[key] = canonical[key]
    return produced, entity
