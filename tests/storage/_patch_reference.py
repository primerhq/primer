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
