"""Render API data as table / json / yaml / name / wide.

``render`` accepts a single object (dict) or a list of objects. Table columns
come from ``derive_columns`` when not supplied. JSON/YAML emit the data
verbatim; ``name`` prints the ``id`` of each row for scripting.
"""

from __future__ import annotations

import io
import json
from typing import Any

import yaml

# Column names we surface first when present (in this order, after id).
_PREFERRED = ("name", "description", "model", "provider", "status", "kind")
# Narrow table excludes container-typed fields.
_CONTAINER_TYPES = {"array", "object"}
_NARROW_MAX = 5
_WIDE_MAX = 12


def derive_columns(entity_schema: dict | None, *, wide: bool) -> list[str]:
    if not entity_schema:
        return ["id"]
    props: dict[str, Any] = entity_schema.get("properties", {})
    scalar = [
        n for n, s in props.items()
        if s.get("type") not in _CONTAINER_TYPES
    ]
    ordered: list[str] = []
    if "id" in props:
        ordered.append("id")
    for pref in _PREFERRED:
        if pref in scalar and pref not in ordered:
            ordered.append(pref)
    for n in scalar:
        if n not in ordered:
            ordered.append(n)
    limit = _WIDE_MAX if wide else _NARROW_MAX
    return ordered[:limit] or ["id"]


def _as_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    return [data]


def render(data: Any, *, fmt: str, columns: list[str] | None = None) -> str:
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False)
    if fmt == "yaml":
        return yaml.safe_dump(data, sort_keys=False).rstrip("\n")
    if fmt == "name":
        return "\n".join(str(r.get("id", "")) for r in _as_rows(data))
    if fmt in ("table", "wide"):
        return _render_table(_as_rows(data), columns)
    raise ValueError(f"unknown output format {fmt!r}")


def _render_table(rows: list[dict], columns: list[str] | None) -> str:
    from rich.console import Console
    from rich.table import Table

    if columns is None:
        keys: list[str] = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        columns = keys or ["id"]
    table = Table(show_edge=False, pad_edge=False)
    for col in columns:
        table.add_column(col.upper())
    for r in rows:
        table.add_row(*[_cell(r.get(c)) for c in columns])
    buf = io.StringIO()
    Console(file=buf, width=200).print(table)
    return buf.getvalue().rstrip("\n")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
