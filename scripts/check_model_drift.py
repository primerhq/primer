"""Flag stored-model shape changes that need a migration and have none.

Primer's entity storage is schemaless JSONB, so nothing at the database
level stops a model's shape from changing under rows that were already
written. Comparing the ``MIGRATIONS`` registry between two commits proves
nothing: three drifts shipped across the session cutover with that
registry untouched, and each one made an existing install unreadable.

Two rules catch all three:

* **A field added as required** breaks every row that predates it.
  ``Collection.search`` gained required ``embedder`` and
  ``vector_store_provider_id``; ``Document`` gained a required ``slug``.
* **A field removed from a class that forbids extras** breaks every row
  that still carries it. ``ChatConfig`` sets ``extra="forbid"``, so the
  retired ``default_agent`` / ``allowed_agents`` / ``allow_agent_switch``
  keys raised on load. The same removal on an ``extra="ignore"`` class is
  harmless, which is why the policy has to be read per class rather than
  assumed.

Parsing is done with :mod:`ast` rather than a regex sweep. A regex over
``Field(`` misses required-ness whenever the call wraps onto the next
line, which is the house style for anything with a description, and it
silently under-reports.

Usage::

    python scripts/check_model_drift.py --base v0.5.0
    python scripts/check_model_drift.py --base v0.5.0 --head HEAD --json

Exit status is 1 when a breaking change is found, 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass, field

#: Stored entities do not all live under primer/model/. Workspace is
#: declared in primer/int/, and WorkspaceRow is only an alias for it.
SEARCH_PATHS: tuple[str, ...] = ("primer/model/", "primer/int/workspace.py")

#: Only persisted shapes can break a stored row, so the report is limited to
#: them. The roots are whatever is handed to ``get_storage(...)``; the rest of
#: the set is everything reachable from a root by annotation, which is how
#: nested blocks like ChatConfig and CollectionSearchConfig get included. A
#: request DTO that merely lives in primer/model/ is not in scope and would
#: otherwise bury the real findings.


@dataclass
class ClassShape:
    """The part of a model's shape that can break an existing row."""

    name: str
    path: str
    forbids_extra: bool = False
    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)

    @property
    def fields(self) -> set[str]:
        return self.required | self.optional


def _stored_roots(ref: str) -> set[str]:
    """Class names passed to ``get_storage(...)`` anywhere in the tree."""
    proc = subprocess.run(
        ["git", "grep", "-hoE", r"get_storage\(([A-Z]\w+)\)", ref],
        capture_output=True, text=True,
    )
    roots: set[str] = set()
    for line in proc.stdout.split():
        if line.startswith("get_storage("):
            roots.add(line[len("get_storage("):].rstrip(")"))
    return roots


def _annotation_names(node: ast.AST) -> set[str]:
    """Every bare identifier used inside an annotation expression."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            # Forward references: `search: "CollectionSearchConfig | None"`.
            out.update(w for w in sub.value.replace("|", " ").split() if w[:1].isupper())
    return out


def _reachable(roots: set[str], edges: dict[str, set[str]]) -> set[str]:
    """Transitive closure of roots over the annotation graph."""
    seen: set[str] = set()
    stack = [r for r in roots]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(edges.get(name, set()) - seen)
    return seen


def _git_show(ref: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def _git_files(ref: str, prefix: str) -> list[str]:
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, prefix],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.split() if p.endswith(".py")]


def _is_required(node: ast.AnnAssign) -> bool:
    """A pydantic field is required when it has no usable default.

    Three shapes count as required: a bare annotation, ``Field(...)`` with
    Ellipsis as the first positional argument, and ``Field()`` carrying
    neither ``default`` nor ``default_factory``.
    """
    if node.value is None:
        return True
    value = node.value
    if isinstance(value, ast.Constant) and value.value is Ellipsis:
        return True
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    name = getattr(func, "id", None) or getattr(func, "attr", None)
    if name != "Field":
        return False
    for arg in value.args:
        if isinstance(arg, ast.Constant) and arg.value is Ellipsis:
            return True
    keywords = {kw.arg for kw in value.keywords}
    if "default" in keywords or "default_factory" in keywords:
        return False
    # Field() with only metadata (description, min_length) and no default
    # is required, the same as a bare annotation.
    return not value.args


def _forbids_extra(cls: ast.ClassDef) -> bool:
    """True when the class sets ``extra="forbid"`` in its model_config."""
    for stmt in cls.body:
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign)
            else [stmt.target] if isinstance(stmt, ast.AnnAssign)
            else []
        )
        names = {getattr(t, "id", None) for t in targets}
        if "model_config" not in names:
            continue
        value = getattr(stmt, "value", None)
        if not isinstance(value, ast.Call):
            continue
        for kw in value.keywords:
            if kw.arg == "extra" and isinstance(kw.value, ast.Constant):
                if kw.value.value == "forbid":
                    return True
    return False


def shapes_at(ref: str) -> tuple[dict[str, ClassShape], dict[str, set[str]]]:
    """Model classes at ``ref``, plus the class -> annotated-types graph."""
    paths: list[str] = []
    for entry in SEARCH_PATHS:
        paths.extend(_git_files(ref, entry) if entry.endswith("/") else [entry])

    out: dict[str, ClassShape] = {}
    edges: dict[str, set[str]] = {}
    for path in paths:
        src = _git_show(ref, path)
        if src is None:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            shape = ClassShape(
                name=node.name, path=path, forbids_extra=_forbids_extra(node),
            )
            refs: set[str] = set()
            for base in node.bases:
                refs |= _annotation_names(base)
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                target = getattr(stmt.target, "id", None)
                if target is None or target.startswith("_"):
                    continue
                if target == "model_config":
                    continue
                if stmt.annotation is not None:
                    refs |= _annotation_names(stmt.annotation)
                if _is_required(stmt):
                    shape.required.add(target)
                else:
                    shape.optional.add(target)
            edges.setdefault(node.name, set()).update(refs)
            # A name declared twice across files is ambiguous; keep the
            # first and let the report show its path.
            out.setdefault(node.name, shape)
    return out, edges


def diff(base: str, head: str) -> tuple[list[dict], list[dict]]:
    """Return (breaking, benign) shape changes between two refs."""
    old, old_edges = shapes_at(base)
    new, new_edges = shapes_at(head)

    # Scope: persisted roots and everything nested inside them, on either ref.
    in_scope = (
        _reachable(_stored_roots(head), new_edges)
        | _reachable(_stored_roots(base), old_edges)
    )
    return compare(
        {k: v for k, v in old.items() if k in in_scope},
        {k: v for k, v in new.items() if k in in_scope},
    )


def compare(
    old: dict[str, ClassShape], new: dict[str, ClassShape],
) -> tuple[list[dict], list[dict]]:
    """Pure rule layer: (breaking, benign) for two already-scoped shape sets."""
    breaking: list[dict] = []
    benign: list[dict] = []
    for name in sorted(set(old) & set(new)):
        o, n = old[name], new[name]
        added_required = sorted(n.required - o.fields)
        became_required = sorted(n.required & o.optional)
        removed = sorted(o.fields - n.fields)

        for f in added_required + became_required:
            breaking.append({
                "model": name, "path": n.path, "field": f,
                "rule": "field is required and did not exist before"
                        if f in added_required
                        else "existing optional field became required",
            })
        for f in removed:
            entry = {"model": name, "path": n.path, "field": f}
            if n.forbids_extra:
                breaking.append({
                    **entry,
                    "rule": 'field removed from a class with extra="forbid"',
                })
            else:
                benign.append({
                    **entry,
                    "rule": 'field removed, but the class ignores extras',
                })

    # A class that disappears is the case a both-refs comparison cannot see,
    # and it is not hypothetical: CollectionSearch became
    # CollectionSearchConfig with two newly-required fields, which is what
    # made every collection unreadable. Rows written under the old name are
    # still on disk, so the disappearance has to be surfaced even though
    # there is no counterpart to diff against.
    for name in sorted(set(old) - set(new)):
        o = old[name]
        breaking.append({
            "model": name, "path": o.path, "field": "*",
            "rule": "class no longer exists; rows written under it still do. "
                    "If it was renamed, diff the replacement by hand",
        })

    return breaking, benign


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="ref the deployed data was written by")
    ap.add_argument("--head", default="HEAD", help="ref being released")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    breaking, benign = diff(args.base, args.head)

    if args.json:
        print(json.dumps({"breaking": breaking, "benign": benign}, indent=2))
        return 1 if breaking else 0

    if benign:
        print(f"{len(benign)} benign change(s), no migration needed:")
        for b in benign:
            print(f"  {b['model']}.{b['field']}: {b['rule']}")
        print()

    if not breaking:
        print(f"OK: no breaking model drift between {args.base} and {args.head}.")
        return 0

    print(f"{len(breaking)} breaking change(s) between {args.base} and {args.head}:")
    for b in breaking:
        print(f"  {b['model']}.{b['field']}  ({b['path']})")
        print(f"      {b['rule']}")
    print()
    print("Each needs a migration that rewrites existing rows, or the change")
    print("must be made backward-compatible. See primer/storage/migrations/.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
