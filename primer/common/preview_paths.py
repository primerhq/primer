"""The path language of the Inbox approval preview allowlist (design note 01a11cd3-66b0, slice 1).

An approval card shows the arguments of the call it asks about. WHICH of them it may draw is declared as DOTTED PATHS into the argument object (``path``,
``entity.id``, ``entity.model.profile_id``) by the tool (``Tool.preview_args``) or by the operator's approval policy (``ToolApprovalPolicy.preview_args``). A path
allows its whole subtree, and a list is transparent: ``entity.nodes.agent_id`` is the ``agent_id`` of every node. Everything not allowed is withheld.

This module is a LEAF (it imports nothing from primer) and knows only the language:

* :func:`path_syntax_error`: is a path well formed;
* :func:`schema_has_path` / :func:`missing_paths`: does a tool's JSON Schema have it (a path that names nothing is a typo, and a typo hides more than was meant);
* :func:`closed_set_names`: the top-level arguments that are safe to show with NO declaration (design ruling D2): a boolean, an integer, a number, ``null``, an
  enum or a const cannot carry a free-form secret. A string, an object, an array, a schema without a type and a ``$ref`` that does not resolve are hidden;
* :func:`classify`: at run time, is the value at a position shown whole, shown in part (an allowed path goes deeper) or hidden.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

MAX_PATH_CHARS = 200
MAX_PATHS = 64
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*")
_CLOSED_TYPES = frozenset({"boolean", "integer", "number", "null"})
_MAX_REF_DEPTH = 12


def path_syntax_error(path: Any) -> str | None:
    """A sentence saying why ``path`` is not a well formed preview path, or ``None``. Segments are names joined by single dots."""
    if not isinstance(path, str):
        return f"preview_args entries are text, got {type(path).__name__}"
    shown = path if len(path) <= 40 else path[:37] + "..."
    if not path:
        return "preview_args has an empty path"
    if len(path) > MAX_PATH_CHARS:
        return f"preview_args path {shown!r} is longer than {MAX_PATH_CHARS} characters"
    for segment in path.split("."):
        if not _SEGMENT.fullmatch(segment):
            return (
                f"preview_args path {shown!r} is not a dotted path of argument names "
                "(letters, digits, '_' and '-', joined by single dots; a list needs no brackets)"
            )
    return None


def _resolve(node: Any, root: dict, depth: int = 0) -> Any:
    """``node`` with a local ``$ref`` followed (``#/$defs/X`` or ``#/definitions/X``), or ``None`` when it cannot be resolved or loops."""
    while isinstance(node, dict) and "$ref" in node:
        if depth >= _MAX_REF_DEPTH:
            return None
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        target: Any = root
        for part in ref[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            return None
        node, depth = target, depth + 1
    return node if isinstance(node, dict) else None


def _is_closed(node: Any, root: dict, depth: int = 0) -> bool:
    """Whether a value of this schema can only be one of a small closed set of non-text values."""
    node = _resolve(node, root)
    if node is None or depth > _MAX_REF_DEPTH:
        return False
    if "const" in node or "enum" in node:
        return True
    declared = node.get("type")
    if isinstance(declared, str):
        return declared in _CLOSED_TYPES
    if isinstance(declared, list):
        return bool(declared) and all(t in _CLOSED_TYPES for t in declared)
    for key in ("anyOf", "oneOf", "allOf"):
        branches = node.get(key)
        if isinstance(branches, list) and branches:
            return all(_is_closed(b, root, depth + 1) for b in branches)
    return False


def closed_set_names(schema: dict) -> list[str]:
    """The top-level arguments of ``schema`` safe to show with no declaration, in schema order (design ruling D2)."""
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    return [name for name, sub in properties.items() if _is_closed(sub, schema)]


def _children(node: Any, segment: str, root: dict, depth: int = 0) -> list[dict]:
    """The schemas a member called ``segment`` of a value of ``node``'s schema can have. A list is transparent (its items are looked in), a union is the union of
    its branches, and an object with no ``properties`` at all is free-form: any member name exists and its schema is anything."""
    node = _resolve(node, root)
    if node is None or depth > _MAX_REF_DEPTH:
        return []
    if not node:
        return [{}]         # the empty schema says nothing about its value: any member name exists
    found: list[dict] = []
    for key in ("anyOf", "oneOf", "allOf"):
        branches = node.get(key)
        if isinstance(branches, list):
            for branch in branches:
                found.extend(_children(branch, segment, root, depth + 1))
    items = node.get("items")
    if isinstance(items, dict):
        found.extend(_children(items, segment, root, depth + 1))
    properties = node.get("properties")
    if isinstance(properties, dict):
        if segment in properties:
            sub = properties[segment]
            found.append(sub if isinstance(sub, dict) else {})
    elif node.get("type") == "object" or "additionalProperties" in node:
        found.append({})
    extra = node.get("additionalProperties")
    if isinstance(extra, dict) and isinstance(properties, dict):
        found.append(extra)
    return found


def schema_has_path(schema: dict, path: str) -> bool:
    """Whether ``path`` names an argument (or a member of one) in the tool's JSON Schema. The path may stop anywhere: it then allows the whole subtree."""
    if not isinstance(schema, dict) or path_syntax_error(path) is not None:
        return False
    nodes: list[dict] = [schema]
    for segment in path.split("."):
        next_nodes: list[dict] = []
        for node in nodes:
            next_nodes.extend(_children(node, segment, schema))
        if not next_nodes:
            return False
        nodes = next_nodes
    return True


def missing_paths(schema: dict, paths: Iterable[str]) -> list[str]:
    """The paths of ``paths`` the schema does not have, each once, in the order given."""
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        if not schema_has_path(schema, path):
            out.append(path)
    return out


def classify(allowed: Sequence[str], here: str) -> Literal["all", "part", "none"]:
    """At run time: the value at ``here`` is shown whole (``all``: an allowed path is ``here`` or an ancestor), in part (``part``: an allowed path goes deeper) or hidden
    (``none``). Compared by whole segments, so ``mode`` never allows ``model``."""
    deeper = False
    prefix = here + "."
    for path in allowed:
        if path == here or here.startswith(path + "."):
            return "all"
        if path.startswith(prefix):
            deeper = True
    return "part" if deeper else "none"


__all__ = ["MAX_PATHS", "MAX_PATH_CHARS", "classify", "closed_set_names", "missing_paths", "path_syntax_error", "schema_has_path"]
