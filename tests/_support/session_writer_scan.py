"""Whole-document writers of ``Storage[WorkspaceSession]``, found by following the HANDLE TYPE.

``tests/session/test_session_writer_set.py`` pins the result of :func:`scan` against
``tests/session/session_writers.txt``. This module is pure ``ast`` over source files: it imports
nothing from ``primer``, reads nothing but ``*.py`` under the root it is given, and its output is
sorted, so two runs over the same tree are byte-identical. ``python -m tests._support.session_writer_scan
[<package dir>]`` prints the result as JSON (default: the repo's ``primer/``), for reading a diff by eye.

What a handle is
================
A *session handle* is any expression that evaluates to ``Storage[WorkspaceSession]``. The scan tracks
a set of model names per expression (``Storage[Harness]`` is a handle too, it is just not a session
one), flow-insensitively, to a fixpoint over the whole package:

* ``<expr>.get_storage(WorkspaceSession)`` under any local, attribute or closure name, and under any
  import alias of the model (``import ... WorkspaceSession as _WorkspaceSession``);
* a parameter annotated ``Storage[WorkspaceSession]`` (quoted or not, ``Optional`` or not), a class
  attribute annotated so, and ``self.x = <handle>`` in any method of the class (or a base class);
* a parameter with no usable annotation (none, ``Any``, bare ``Storage``) NAMED like a session store:
  ``*session_storage*``, ``*sessions_storage*``, ``*session_store*`` or exactly ``sessions``; any
  attribute chain whose last component is named so;
* a parameter whose default is ``Depends(<fn>)`` where ``<fn>`` returns a handle;
* a call of a function that returns a handle (found by return annotation or ``return <handle>``, matched
  by the callee's simple name across the package);
* aliases (``b = a``), closures (a nested function sees its enclosing functions' handles), and call
  sites: when a call passes a handle as an argument, the callee's matching parameter is a handle (callees
  are matched by simple name, over-approximating; a constructor call feeds ``__init__``).

What is a writer
================
``update``, ``update_unless``, ``upsert``, ``replace`` and ``put`` on a session handle. They are
reported at the INNERMOST enclosing function's qualified name (``Class.method`` or ``outer.inner``), so
a nested closure is not also attributed to the function that defines it. A bare attribute reference
(``asyncio.to_thread(sessions.update, row)``) and ``getattr(handle, "update")`` count like a call.
``Storage`` today has ``update`` and ``update_unless`` only; the other names guard against the API
growing. ``create`` inserts a new row and never overwrites one, so it is not a writer.

DECISION, ``patch_if`` / ``patch_if_checked``: the pin is about WHOLE-DOCUMENT writers, the writes
that can erase a concurrent field change (a park committed between a get and an update). A
field-scoped ``patch_if`` cannot, so it is NOT in the pinned set. The sites are still collected, in
``ScanResult.patches``, so a reader of the scan can see the converted writers; the pin test does not
compare them. ``patch_if_checked(handle, ...)`` (``primer.storage.cas``) takes the handle as its first
argument and is reported there as ``patch_if_checked``. ``delete`` is reported separately
(``ScanResult.deletes``) and pinned as a regression guard.

Unresolved candidates
=====================
A call ``<receiver>.update(...)`` / ``.update_unless(...)`` whose receiver has NO known storage type
is a candidate session writer the scan could not resolve. To keep ``dict.update`` and friends out, a
candidate also needs the whole-document shape: the first argument is a ``.model_copy(...)`` call (or a
local assigned from one), or an identifier of the receiver or the argument contains ``session``. A
receiver typed as ``Storage[<another model>]`` is positively classified and never a candidate. The pin
test makes every candidate carry a hand-written reason, so a NEW unresolved receiver fails until
someone looks at it. A candidate's ``method`` is ``<receiver>.<method>`` (``storage.update``).

Limits (the scan is a tripwire, not a proof)
============================================
It cannot see a handle that reaches a write through a container (``handles["s"].update``), through a
dynamic ``getattr(handle, name)`` with a non-literal name, through a function that returns a handle
only via a ``@property`` or a ``functools.partial``, through two classes sharing an attribute name
without a base-class link, through a class that subclasses ``Storage[WorkspaceSession]`` and writes via
``self`` / ``super()``, or through a call passed to a callee that the name match misses (an aliased
import, ``*args`` / ``**kwargs`` forwarding, a callback table). It scans ``primer/`` only (nothing under
``scripts/`` or ``runtime/`` references ``WorkspaceSession`` today). Such a write is caught only when
its receiver or argument LOOKS like a session or a whole-document copy (the unresolved rule above).
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re
import sys
from collections import defaultdict
from typing import Any

SESSION_MODEL = "WorkspaceSession"
WRITE_METHODS = frozenset({"update", "update_unless", "upsert", "replace", "put"})
DELETE_METHODS = frozenset({"delete"})
PATCH_METHODS = frozenset({"patch_if"})
PATCH_FUNCTIONS = frozenset({"patch_if_checked"})
UNRESOLVED_METHODS = frozenset({"update", "update_unless"})

_TRACKED_METHODS = WRITE_METHODS | DELETE_METHODS | PATCH_METHODS
_HANDLE_NAME = re.compile(r"session_storage|sessions_storage|session_store")
_UNTYPED_ANNOTATIONS = frozenset(
    {"Any", "object", "Storage", "Any | None", "Optional[Any]", "Storage | None",
     "Optional[Storage]"}
)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def default_root() -> pathlib.Path:
    """The repo's ``primer/`` package, located from THIS file (not the cwd)."""
    return pathlib.Path(__file__).resolve().parents[2] / "primer"


@dataclasses.dataclass(frozen=True, order=True)
class Site:
    """One call site. ``line`` is informational only: it is never part of a pin key."""

    file: str
    function: str
    method: str
    line: int

    def key(self) -> tuple[str, str, str]:
        return (self.file, self.function, self.method)


@dataclasses.dataclass(frozen=True)
class ScanResult:
    writers: tuple[Site, ...]
    deletes: tuple[Site, ...]
    patches: tuple[Site, ...]
    unresolved: tuple[Site, ...]

    def counts(self, kind: str) -> dict[tuple[str, str, str], int]:
        """Sites per ``(file, function, method)`` key, sorted by key."""
        out: dict[tuple[str, str, str], int] = defaultdict(int)
        for site in getattr(self, kind):
            out[site.key()] += 1
        return dict(sorted(out.items()))

    def as_json(self) -> dict[str, list[list[Any]]]:
        return {
            kind: [[s.file, s.line, s.function, s.method] for s in getattr(self, kind)]
            for kind in ("writers", "deletes", "patches", "unresolved")
        }


def scan(root: pathlib.Path) -> ScanResult:
    """Scan every ``*.py`` under ``root`` (a package directory); files are named ``<root.name>/...``."""
    return _Analysis(root).run()


# ---------------------------------------------------------------------------------------------
# small syntax helpers


def _expr_key(node: ast.AST) -> str | None:
    """``a`` / ``a.b.c`` for a Name / Attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_key(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _bucket(method: str) -> str:
    if method in WRITE_METHODS:
        return "writers"
    return "deletes" if method in DELETE_METHODS else "patches"


def _is_model_copy(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_copy"
    )


def _mentions_session(*nodes: ast.AST) -> bool:
    for root in nodes:
        for n in ast.walk(root):
            ident = n.id if isinstance(n, ast.Name) else getattr(n, "attr", "")
            if isinstance(ident, str) and "session" in ident.lower():
                return True
    return False


def _own_nodes(scope: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
    """Nodes owned by ``scope`` (not by a nested def or class), and the nested defs and classes."""
    stack = list(reversed(scope.body))  # type: ignore[attr-defined]
    nodes: list[ast.AST] = []
    nested: list[ast.AST] = []
    while stack:
        node = stack.pop()
        if isinstance(node, (*_DEFS, ast.ClassDef)):
            nested.append(node)
            continue
        nodes.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return nodes, nested


# ---------------------------------------------------------------------------------------------
# the analysis


class _Scope:
    """A module, class body or function: the unit that owns an environment of typed expressions."""

    def __init__(self, node, qualname, parent, cls_key, mod, *, is_class=False, is_method=False):
        self.node = node
        self.qualname = qualname
        self.parent = parent  # enclosing function or module scope (class scopes are skipped)
        self.cls_key = cls_key  # (file, class name) when this is, or is nested in, a class
        self.mod = mod
        self.is_class = is_class
        self.is_method = is_method
        self.own, self.nested = _own_nodes(node)
        self.assigns: list[ast.AST] = []
        self.calls: list[ast.Call] = []
        self.returns: list[ast.Return] = []
        for n in self.own:
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                self.assigns.append(n)
            elif isinstance(n, ast.Call):
                self.calls.append(n)
            elif isinstance(n, ast.Return) and n.value is not None:
                self.returns.append(n)
        self.copy_names = {
            t.id
            for n in self.assigns
            if isinstance(n, ast.Assign) and _is_model_copy(n.value)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        self.env: dict[str, set[str]] = {}

    @property
    def is_function(self) -> bool:
        return isinstance(self.node, _DEFS)

    def params(self) -> list[ast.arg]:
        a = self.node.args
        return [*a.posonlyargs, *a.args, *a.kwonlyargs]

    def positional(self) -> list[str]:
        a = self.node.args
        return [x.arg for x in (*a.posonlyargs, *a.args)]


class _Module:
    def __init__(self, rel: str, tree: ast.Module):
        self.rel = rel
        self.aliases = {SESSION_MODEL}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == SESSION_MODEL:
                        self.aliases.add(a.asname or a.name)
        for n in tree.body:  # `_WS = WorkspaceSession`
            if (
                isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Name)
                and n.value.id in self.aliases
            ):
                self.aliases.update(t.id for t in n.targets if isinstance(t, ast.Name))
        self.scopes: list[_Scope] = []
        self._build(_Scope(tree, "<module>", None, None, self), "")

    def _build(self, scope: _Scope, prefix: str) -> None:
        """Register ``scope`` and, depth first, every def and class nested directly in it."""
        self.scopes.append(scope)
        # a method does not see its class body's names: it sees the scope around the class
        env_parent = scope.parent if scope.is_class else scope
        for child in scope.nested:
            name = f"{prefix}.{child.name}" if prefix else child.name
            if isinstance(child, ast.ClassDef):
                sub = _Scope(
                    child, f"{name}.<body>", env_parent, (self.rel, child.name), self,
                    is_class=True,
                )
            else:
                cls_key = (self.rel, scope.node.name) if scope.is_class else scope.cls_key
                sub = _Scope(
                    child, name, env_parent, cls_key, self, is_method=scope.is_class
                )
            self._build(sub, name)


class _Analysis:
    def __init__(self, root: pathlib.Path):
        self.modules: list[_Module] = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root.parent).as_posix()
            self.modules.append(_Module(rel, ast.parse(path.read_text(encoding="utf-8"))))
        self.param_types: defaultdict[tuple[int, str], set[str]] = defaultdict(set)
        self.class_attrs: defaultdict[tuple[str, str], defaultdict[str, set[str]]] = (
            defaultdict(lambda: defaultdict(set))
        )
        self.returners: defaultdict[str, set[str]] = defaultdict(set)
        self.bases: dict[tuple[str, str], list[str]] = {}
        self.classes_by_name: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
        self.funcs_by_name: defaultdict[str, list[_Scope]] = defaultdict(list)
        self.ctors_by_name: defaultdict[str, list[_Scope]] = defaultdict(list)
        for mod in self.modules:
            for sc in mod.scopes:
                if sc.is_class:
                    key = sc.cls_key
                    self.classes_by_name[key[1]].append(key)
                    self.bases[key] = [
                        b for b in map(_callee_name, sc.node.bases) if b is not None
                    ]
                elif sc.is_function:
                    self.funcs_by_name[sc.node.name].append(sc)
                    if sc.is_method and sc.node.name == "__init__":
                        self.ctors_by_name[sc.cls_key[1]].append(sc)
        self._changed = False

    # -- fixpoint -------------------------------------------------------------------------

    def run(self) -> ScanResult:
        while True:
            self._changed = False
            for mod in self.modules:
                for sc in mod.scopes:
                    self._analyse(sc)
            if not self._changed:
                break
        buckets: dict[str, list[Site]] = {
            "writers": [], "deletes": [], "patches": [], "unresolved": [],
        }
        for mod in self.modules:
            for sc in mod.scopes:
                self._collect(sc, buckets)
        return ScanResult(*(tuple(sorted(set(buckets[k]))) for k in
                            ("writers", "deletes", "patches", "unresolved")))

    def _add(self, target: set[str], types: set[str]) -> bool:
        if types and not types <= target:
            target |= types
            self._changed = True
            return True
        return False

    def _attrs_of(self, cls_key, seen=None) -> dict[str, set[str]]:
        seen = seen if seen is not None else set()
        if cls_key in seen:
            return {}
        seen.add(cls_key)
        merged = {k: set(v) for k, v in self.class_attrs.get(cls_key, {}).items()}
        for base in self.bases.get(cls_key, []):
            for base_key in self.classes_by_name.get(base, []):
                for attr, types in self._attrs_of(base_key, seen).items():
                    merged.setdefault(attr, set()).update(types)
        return merged

    # -- type evaluation ------------------------------------------------------------------

    def _model_name(self, node: ast.AST, mod: _Module) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value.strip()
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            return None
        return SESSION_MODEL if name in mod.aliases else name

    def _ann_types(self, ann: ast.AST | None, mod: _Module) -> set[str]:
        """Model names M for every ``...Storage[M]`` inside an annotation (quoted forms parsed)."""
        out: set[str] = set()
        if ann is None:
            return out
        for n in ast.walk(ann):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "Storage[" in n.value:
                try:
                    out |= self._ann_types(ast.parse(n.value, mode="eval").body, mod)
                except SyntaxError:
                    pass
            elif isinstance(n, ast.Subscript):
                outer = _callee_name(n.value) or ""
                if outer.endswith("Storage"):
                    elts = n.slice.elts if isinstance(n.slice, ast.Tuple) else [n.slice]
                    out |= {m for e in elts if (m := self._model_name(e, mod))}
        return out

    def _ev(self, e: ast.AST, env: dict[str, set[str]], mod: _Module) -> set[str]:
        if isinstance(e, ast.Await):
            return self._ev(e.value, env, mod)
        if isinstance(e, ast.IfExp):
            return self._ev(e.body, env, mod) | self._ev(e.orelse, env, mod)
        if isinstance(e, ast.BoolOp):
            return set().union(*(self._ev(v, env, mod) for v in e.values))
        key = _expr_key(e)
        if key is not None:
            return env.get(key) or (
                {SESSION_MODEL} if _HANDLE_NAME.search(key.rsplit(".", 1)[-1]) else set()
            )
        if isinstance(e, ast.Call):
            f = e.func
            if isinstance(f, ast.Attribute) and f.attr == "get_storage":
                m = self._model_name(e.args[0], mod) if e.args else None
                return {m} if m else set()
            name = _callee_name(f)
            if name == "Depends" and e.args:
                name = _callee_name(e.args[0])
            if name:
                return self.returners.get(name, set())
        return set()

    # -- per-scope analysis ---------------------------------------------------------------

    def _seed_params(self, sc: _Scope) -> None:
        a = sc.node.args
        positional = [*a.posonlyargs, *a.args]
        defaults: dict[str, ast.AST] = dict(
            zip((p.arg for p in positional[len(positional) - len(a.defaults):]), a.defaults,
                strict=True)
        )
        defaults.update(
            {p.arg: d for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None}
        )
        for p in sc.params():
            types = self._ann_types(p.annotation, sc.mod)
            if p.arg in defaults:
                types |= self._ev(defaults[p.arg], sc.env, sc.mod)
            untyped = (
                p.annotation is None
                or ast.unparse(p.annotation).strip("'\"") in _UNTYPED_ANNOTATIONS
            )
            named_like_a_store = _HANDLE_NAME.search(p.arg) or p.arg == "sessions"
            if not types and untyped and named_like_a_store:
                types.add(SESSION_MODEL)
            types |= self.param_types.get((id(sc.node), p.arg), set())
            if types:
                sc.env[p.arg] = types
            else:
                sc.env.pop(p.arg, None)

    def _analyse(self, sc: _Scope) -> None:
        mod = sc.mod
        env = {k: set(v) for k, v in (sc.parent.env if sc.parent else {}).items()}
        sc.env = env
        if sc.cls_key and not sc.is_class:
            for attr, types in self._attrs_of(sc.cls_key).items():
                env[f"self.{attr}"] = set(types)
                env[f"cls.{attr}"] = set(types)
        if sc.is_function:
            self._seed_params(sc)
        while True:  # assignments, flow-insensitive, to a fixpoint
            grew = False
            for n in sc.assigns:
                if isinstance(n, ast.Assign):
                    targets, types = n.targets, self._ev(n.value, env, mod)
                elif isinstance(n, ast.AnnAssign):
                    targets = [n.target]
                    types = self._ann_types(n.annotation, mod)
                    if n.value is not None:
                        types |= self._ev(n.value, env, mod)
                else:
                    targets, types = [n.target], self._ev(n.value, env, mod)
                if not types:
                    continue
                for t in targets:
                    key = _expr_key(t)
                    if key is None:
                        continue
                    grew |= not types <= env.setdefault(key, set())
                    env[key] |= types
                    attr = None
                    if sc.cls_key and not sc.is_class and key.startswith(("self.", "cls.")):
                        attr = key.split(".", 1)[1]
                    elif sc.is_class and "." not in key:
                        attr = key
                    if attr and "." not in attr:
                        self._add(self.class_attrs[sc.cls_key][attr], types)
            if not grew:
                break
        if sc.is_function:
            ret = self._ann_types(sc.node.returns, mod)
            for r in sc.returns:
                ret |= self._ev(r.value, env, mod)
            self._add(self.returners[sc.node.name], ret)
        for call in sc.calls:
            self._propagate(sc, call)

    def _propagate(self, sc: _Scope, call: ast.Call) -> None:
        name = _callee_name(call.func)
        if name is None:
            return
        pos = [(i, self._ev(a, sc.env, sc.mod)) for i, a in enumerate(call.args)
               if not isinstance(a, ast.Starred)]
        kws = [(k.arg, self._ev(k.value, sc.env, sc.mod)) for k in call.keywords if k.arg]
        pos = [(i, t) for i, t in pos if t]
        kws = [(k, t) for k, t in kws if t]
        if not pos and not kws:
            return
        targets = list(self.ctors_by_name.get(name, []))
        for t in self.funcs_by_name.get(name, []):
            if isinstance(call.func, ast.Attribute) or not t.is_method:
                targets.append(t)
        for t in targets:
            names = t.positional()
            offset = 1 if t.is_method and names else 0
            for i, types in pos:
                if i + offset < len(names):
                    self._add(self.param_types[(id(t.node), names[i + offset])], types)
            known = {p.arg for p in t.params()}
            for k, types in kws:
                if k in known:
                    self._add(self.param_types[(id(t.node), k)], types)

    # -- collection -----------------------------------------------------------------------

    def _collect(self, sc: _Scope, buckets: dict[str, list[Site]]) -> None:
        mod, env = sc.mod, sc.env

        def site(node: ast.AST, method: str) -> Site:
            return Site(mod.rel, sc.qualname, method, node.lineno)

        for n in sc.own:
            if isinstance(n, ast.Attribute) and n.attr in _TRACKED_METHODS:
                if SESSION_MODEL in self._ev(n.value, env, mod):
                    buckets[_bucket(n.attr)].append(site(n, n.attr))
            elif isinstance(n, ast.Call):
                self._collect_call(sc, n, site, buckets)

    def _collect_call(self, sc, call, site, buckets) -> None:
        mod, env = sc.mod, sc.env
        name = _callee_name(call.func)
        if name in PATCH_FUNCTIONS and call.args:
            if SESSION_MODEL in self._ev(call.args[0], env, mod):
                buckets["patches"].append(site(call, name))
        elif (
            name == "getattr" and isinstance(call.func, ast.Name) and len(call.args) >= 2
            and isinstance(call.args[1], ast.Constant) and call.args[1].value in _TRACKED_METHODS
        ):
            if SESSION_MODEL in self._ev(call.args[0], env, mod):
                method = call.args[1].value
                buckets[_bucket(method)].append(site(call, method))
        elif (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in UNRESOLVED_METHODS
            and call.args
            and not self._ev(call.func.value, env, mod)
        ):
            first = call.args[0]
            whole_document = (
                _is_model_copy(first)
                or (isinstance(first, ast.Name) and first.id in sc.copy_names)
                or _mentions_session(call.func.value, first)
            )
            if whole_document:
                label = _expr_key(call.func.value) or ast.unparse(call.func.value)
                buckets["unresolved"].append(site(call, f"{label}.{call.func.attr}"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = pathlib.Path(args[0]) if args else default_root()
    print(json.dumps(scan(root).as_json(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
