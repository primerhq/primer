"""The backend-independent half of :meth:`primer.int.Storage.patch_if`.

``patch_if`` is a field-scoped compare-and-set: ONE statement, evaluated by the database against the
row's CURRENT document, that merges a shallow ``patch`` and sets nested ``set_paths`` leaves iff every
``where`` clause matches. This module holds what both backends share:

* argument validation (so a malformed spec fails identically on SQLite and Postgres, before any SQL),
* :func:`raw_generation`, the canonical value a caller puts in ``where`` to say "the row I read",
* the two SQL compilers, kept pure (they return text and parameters, they never touch a connection)
  so the exact statements are unit-testable without a database.

Why it exists: a whole-document ``update`` writes a snapshot over everything, so a writer that read the
row a moment ago silently erases a concurrent writer's fields (a cancel flag, a human reply). ``patch_if``
writes only the fields the caller owns, and only if the generation it read is still current.

Semantics of the nested part (identical on both backends)
---------------------------------------------------------
``set_paths`` maps tuple paths to JSON values, e.g. ``("parked_state", "resume_event_payloads", key)``.

1. All parent-ensures run BEFORE any leaf is set, shallowest first. Ensuring a shared parent can then never
   reset a leaf that an earlier path just wrote.
2. A parent is ENSURED when it is an object. A parent that is absent, JSON null or a non-object scalar is
   REPLACED by ``{}``: ``jsonb_set`` / ``json_set`` silently leave the document unchanged when an
   intermediate value is not a container, and a JSON null is not SQL NULL, so ``COALESCE`` alone is wrong.
3. Path elements are bound, never spliced into SQL text (Postgres: a ``text[]`` parameter; SQLite: a
   quoted key inside a ``$`` path, which is why quotes, backslashes and control characters are rejected).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from primer.model.common import dump_for_storage

#: Deepest path ``set_paths`` accepts. The deepest real one is
#: ``("parked_state", "resume_event_payloads", dispatch_key)``, depth 3.
MAX_PATH_DEPTH = 4

_FORBIDDEN_IN_KEY = ('"', "\\")

#: Each distinct parent path is repeated three times in the compiled expression (the ensure is a CASE over
#: the accumulator), so statement size and, on SQLite, bind-parameter count grow as 3^parents. Real callers
#: touch two parents; the cap keeps a malformed spec from building a megabyte statement.
MAX_DISTINCT_PARENTS = 4
MAX_PATCH_KEYS = 32
MAX_LEAVES = 16

#: Scalar types a ``where`` clause may name. ``bool`` is listed before ``int`` wherever order matters,
#: because ``True`` is an ``int`` in Python and must stay a JSON boolean.
_WHERE_SCALARS = (str, int, float, bool, type(None))


def raw_generation(entity: Any, field: str) -> Any:
    """The CANONICAL stored value of ``field`` on ``entity``, for use in a ``where`` clause.

    This is exactly what the storage layer wrote for that field (``dump_for_storage``, the same dump
    ``Storage._to_row`` serialises), not a re-formatted Python value: a ``datetime`` comes back as the
    ISO string the backend holds, so ``read -> raw_generation -> patch_if(where=that)`` is a no-op round
    trip that matches. A caller must never build the comparison value itself (``dt.isoformat()`` differs
    from pydantic's ``Z`` form and would make the compare-and-set reject forever).

    It is "what the backend stores" for a row that was written from a model dump. It is NOT for a field
    whose stored text is not the model's own canonical form: a timestamp written through ``patch`` or by a
    legacy writer as ``+00:00`` (a read re-dumps it as ``Z``), a field absent from an old document that the
    model now defaults, or a field a validator normalises. Guard on such a field only after a canonical
    write, and keep the generation fields (the ones a hook reads back) written by this layer.
    """
    dumped = dump_for_storage(entity)
    if field not in dumped:
        raise ValueError(f"{type(entity).__name__} has no stored field {field!r}")
    return dumped[field]


def _check_key(key: Any, what: str) -> None:
    if not isinstance(key, str) or key == "":
        raise ValueError(f"{what} must be a non-empty string, got {key!r}")
    if any(ch in key for ch in _FORBIDDEN_IN_KEY) or any(ord(ch) < 0x20 for ch in key):
        raise ValueError(
            f"{what} {key!r} contains a quote, a backslash or a control character, which "
            "patch_if rejects on every backend so SQLite and Postgres stay identical"
        )


def _check_json(value: Any, what: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} is not JSON-ready: {exc}") from exc


def validate_patch(
    patch: Mapping[str, Any] | None,
    set_paths: Mapping[tuple[str, ...], Any] | None,
    where: Mapping[str, Sequence[Any]],
) -> tuple[dict[str, Any], dict[tuple[str, ...], Any], dict[str, list[Any]]]:
    """Validate a ``patch_if`` spec and return normalised copies. Raises ``ValueError``."""
    patch_d = dict(patch or {})
    paths_d = dict(set_paths or {})
    where_d: dict[str, list[Any]] = {}

    if not patch_d and not paths_d:
        raise ValueError("patch_if needs a non-empty patch or set_paths")
    if len(patch_d) > MAX_PATCH_KEYS or len(paths_d) > MAX_LEAVES:
        raise ValueError(
            f"patch_if takes at most {MAX_PATCH_KEYS} patch keys and {MAX_LEAVES} set_paths leaves"
        )
    if not where:
        raise ValueError(
            "patch_if needs a non-empty where: an unguarded field-scoped write is Storage.update's job, "
            "and an empty guard would hide a caller that forgot its generation"
        )
    for key, value in patch_d.items():
        _check_key(key, "patch key")
        if key == "id":
            raise ValueError("patch_if cannot change the id")
        _check_json(value, f"patch[{key!r}]")

    for path, value in paths_d.items():
        if not isinstance(path, tuple) or not path:
            raise ValueError(f"set_paths key must be a non-empty tuple, got {path!r}")
        if len(path) > MAX_PATH_DEPTH:
            raise ValueError(
                f"set_paths path {path!r} is deeper than {MAX_PATH_DEPTH}; "
                "patch_if is for shallow field-scoped writes"
            )
        for part in path:
            _check_key(part, "set_paths element")
        if path[0] == "id":
            raise ValueError("patch_if cannot change the id")
        if path[0] in patch_d:
            raise ValueError(
                f"{path[0]!r} is in both patch and set_paths; write it one way or the other"
            )
        _check_json(value, f"set_paths[{path!r}]")
    ordered = sorted(paths_d)
    for a, b in zip(ordered, ordered[1:]):
        if b[: len(a)] == a:
            raise ValueError(f"set_paths {a!r} is a prefix of {b!r}; the write order would decide")
    if len(parent_paths(paths_d)) > MAX_DISTINCT_PARENTS:
        raise ValueError(
            f"set_paths touches more than {MAX_DISTINCT_PARENTS} distinct parent objects; "
            "the compiled statement grows as 3^parents"
        )

    for field, allowed in where.items():
        _check_key(field, "where field")
        if field == "id":
            raise ValueError(
                "where cannot name 'id': the id is a column, not part of the stored document"
            )
        if isinstance(allowed, (str, bytes)) or not isinstance(allowed, Sequence):
            raise ValueError(
                f"where[{field!r}] must be a LIST of allowed values, got {type(allowed).__name__} "
                "(a bare string would be read as a list of its characters and never match)"
            )
        values = list(allowed)
        if not values:
            raise ValueError(f"where[{field!r}] is empty, which can never match")
        for v in values:
            if not isinstance(v, _WHERE_SCALARS):
                raise ValueError(
                    f"where[{field!r}] holds {type(v).__name__}; use a JSON scalar "
                    "(raw_generation gives the canonical value of a field)"
                )
            _check_json(v, f"where[{field!r}]")
        where_d[field] = values
    return patch_d, paths_d, where_d


def _typed_equal(a: Any, b: Any) -> bool:
    """JSON-typed equality, the rule ``where`` follows: ``True`` is not ``1``, ``1`` equals ``1.0``."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def document_matches(doc: Mapping[str, Any], where: Mapping[str, Sequence[Any]]) -> bool:
    """Whether a stored document (``dump_for_storage`` form) satisfies a ``where``, in Python.

    Used only to cross-check the database's verdict (the drift tripwire); the database remains the
    authority on whether a write applies.
    """
    return all(
        any(_typed_equal(doc.get(field), v) for v in allowed)
        for field, allowed in where.items()
    )


def parent_paths(set_paths: Mapping[tuple[str, ...], Any]) -> list[tuple[str, ...]]:
    """Every distinct proper prefix of every path, shallowest first (the ensure order)."""
    parents: set[tuple[str, ...]] = set()
    for path in set_paths:
        for depth in range(1, len(path)):
            parents.add(path[:depth])
    return sorted(parents, key=lambda p: (len(p), p))


# ---------------------------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------------------------


def compile_postgres(
    patch: Mapping[str, Any],
    set_paths: Mapping[tuple[str, ...], Any],
    where: Mapping[str, Sequence[Any]],
    *,
    first_param: int,
) -> tuple[str, str, list[Any]]:
    """Return ``(set_expression, where_sql, params)`` for a Postgres ``UPDATE``.

    Placeholders are numbered from ``$first_param`` (the caller owns ``$1``, the id). The new document
    is built as ONE expression over the row's own ``data`` column, so it is evaluated against the row's
    current version in the same statement as the write: no read-modify-write.
    """
    params: list[Any] = []

    def ph(value: Any, cast: str) -> str:
        params.append(value)
        return f"${first_param + len(params) - 1}::{cast}"

    acc = "data"
    if patch:
        acc = f"(data || {ph(json.dumps(dict(patch), allow_nan=False), 'jsonb')})"
    for parent in parent_paths(set_paths):
        p = ph(list(parent), "text[]")
        acc = (
            f"(CASE WHEN jsonb_typeof({acc} #> {p}) = 'object' THEN {acc} "
            f"ELSE jsonb_set({acc}, {p}, '{{}}'::jsonb, true) END)"
        )
    for path in sorted(set_paths):
        acc = (
            f"jsonb_set({acc}, {ph(list(path), 'text[]')}, "
            f"{ph(json.dumps(set_paths[path], allow_nan=False), 'jsonb')}, true)"
        )

    clauses: list[str] = []
    for field, allowed in where.items():
        f = ph(field, "text")
        parts: list[str] = []
        non_null = [v for v in allowed if v is not None]
        if non_null:
            arr = ph(json.dumps(non_null, allow_nan=False), "jsonb")
            parts.append(f"(data -> {f}) IN (SELECT jsonb_array_elements({arr}))")
        if any(v is None for v in allowed):
            parts.append(f"((data -> {f}) IS NULL OR (data -> {f}) = 'null'::jsonb)")
        clauses.append("(" + " OR ".join(parts) + ")")
    return acc, " AND ".join(clauses), params


# ---------------------------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------------------------


def _sqlite_path(path: Sequence[str]) -> str:
    return "$" + "".join(f'."{part}"' for part in path)


def compile_sqlite(
    patch: Mapping[str, Any],
    set_paths: Mapping[tuple[str, ...], Any],
    where: Mapping[str, Sequence[Any]],
) -> tuple[str, list[Any], str, list[Any]]:
    """Return ``(set_expression, set_params, where_sql, where_params)`` for a SQLite ``UPDATE``.

    SQLite placeholders are positional, so the parameter list must follow the SQL TEXT order, and the
    caller assembles ``set_params + [id] + where_params``. The ensure step is
    ``CASE WHEN json_type(ACC, ?) = 'object' THEN ACC ELSE json_set(ACC, ?, json('{}')) END`` where ACC
    is the accumulator built so far; ACC (and every ``?`` inside it) therefore appears three times and
    its parameters are repeated to match. The count stays small because ``validate_patch`` caps the number
    of distinct parent paths (``MAX_DISTINCT_PARENTS``), patch keys and leaves.
    """
    acc = "data"
    acc_params: list[Any] = []
    for key in patch:
        acc = f"json_set({acc}, ?, json(?))"
        acc_params = acc_params + [_sqlite_path((key,)), json.dumps(patch[key], allow_nan=False)]
    for parent in parent_paths(set_paths):
        path = _sqlite_path(parent)
        acc_text = acc
        acc = (
            f"(CASE WHEN json_type({acc_text}, ?) = 'object' THEN {acc_text} "
            f"ELSE json_set({acc_text}, ?, json('{{}}')) END)"
        )
        # Textual order: json_type(ACC, ?) -> THEN ACC -> ELSE json_set(ACC, ?, ...).
        acc_params = [*acc_params, path, *acc_params, *acc_params, path]
    for leaf in sorted(set_paths):
        acc = f"json_set({acc}, ?, json(?))"
        acc_params = acc_params + [_sqlite_path(leaf), json.dumps(set_paths[leaf], allow_nan=False)]

    where_params: list[Any] = []
    clauses: list[str] = []
    for field, allowed in where.items():
        path = _sqlite_path((field,))
        parts: list[str] = []
        for v in allowed:
            if v is None:
                parts.append("(json_type(data, ?) IS NULL OR json_type(data, ?) = 'null')")
                where_params += [path, path]
            elif isinstance(v, bool):
                parts.append("json_type(data, ?) = ?")
                where_params += [path, "true" if v else "false"]
            elif isinstance(v, (int, float)):
                # One numeric rule on every backend: 1 equals 1.0 (jsonb numeric equality), but a
                # number never equals a string or a boolean.
                parts.append(
                    "(json_type(data, ?) IN ('integer', 'real') AND json_extract(data, ?) = ?)"
                )
                where_params += [path, path, v]
            else:
                parts.append("(json_type(data, ?) = 'text' AND json_extract(data, ?) = ?)")
                where_params += [path, path, v]
        clauses.append("(" + " OR ".join(parts) + ")")
    return acc, acc_params, " AND ".join(clauses), where_params
