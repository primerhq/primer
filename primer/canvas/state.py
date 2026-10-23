"""The canvas state and the atomic batch (Pivot P5, Task 2).

``CanvasState`` is the stored form (research report d.7): a flat, id-keyed
``elements`` map, the live ``values`` of the controls (with who set each), and a
``rev`` the service owns. ``apply_batch`` takes a state and a list of Canvas Lang
statements and either returns the new state or refuses the whole batch with an
error for every bad line: it never mutates its input and never touches ``rev``.

Pure: no I/O, no clock.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from primer.canvas.catalog import (
    CATALOG,
    HTML_VALUE_MAX_CHARS,
    ElementSpec,
    Param,
    all_params,
    has_unstorable_char,
    param_for,
    property_help,
    signature,
    validate_arg,
)
from primer.canvas.lang import (
    KEEP,
    MAX_DEPTH,
    MAX_ERRORS,
    MAX_FLOAT,
    MAX_INT,
    MSG_BARE_ID,
    MSG_NUMBER_RANGE,
    MSG_TOO_DEEP,
    ROOT_ID,
    Append,
    Assign,
    Clear,
    Delete,
    LangError,
    Ref,
    SetProp,
    Statement,
    clip,
    id_problem,
)
from primer.model.canvas import ROOT_ELEMENT  # re-exported: the model owns the constant, the batch module is where it is used

_GONE = object()
_RESET_WARNING = "{id}: the user's value was reset because the element changed"


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality: like ``==`` but a boolean never equals a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


@dataclass
class CanvasState:
    """``{rev, elements, values}`` plus who set each value ("user" or "agent")."""

    rev: int
    elements: dict[str, dict[str, Any]]
    values: dict[str, Any]
    value_set_by: dict[str, str]

    @classmethod
    def empty(cls) -> CanvasState:
        return cls(0, {ROOT_ID: copy.deepcopy(ROOT_ELEMENT)}, {}, {})

    def copy(self) -> CanvasState:
        return CanvasState(self.rev, copy.deepcopy(self.elements), copy.deepcopy(self.values), dict(self.value_set_by))

    def to_json(self) -> dict[str, Any]:
        return {
            "rev": self.rev,
            "elements": copy.deepcopy(self.elements),
            "values": copy.deepcopy(self.values),
            "value_set_by": dict(self.value_set_by),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CanvasState:
        elements = copy.deepcopy(dict(data.get("elements") or {}))
        elements.setdefault(ROOT_ID, copy.deepcopy(ROOT_ELEMENT))
        return cls(
            int(data.get("rev") or 0),
            elements,
            copy.deepcopy(dict(data.get("values") or {})),
            dict(data.get("value_set_by") or {}),
        )

    def current_value(self, eid: str) -> Any:
        """The live value: what a user or the agent set, else the element's own ``value`` prop."""
        if eid in self.values:
            return self.values[eid]
        return self.elements.get(eid, {}).get("props", {}).get("value")

    def element_count(self) -> int:
        """Elements other than the root."""
        return len(self.elements) - (1 if ROOT_ID in self.elements else 0)

    def size_bytes(self) -> int:
        """UTF-8 length of the compact JSON of the elements and the values."""
        text = json.dumps({"elements": self.elements, "values": self.values}, separators=(",", ":"), ensure_ascii=False)
        return len(text.encode("utf-8"))


@dataclass(frozen=True)
class Limits:
    max_elements: int = 200
    max_html_bytes: int = 65536
    max_canvas_bytes: int = 524288


@dataclass(frozen=True)
class Changed:
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def summary(self) -> str:
        return f"+{len(self.added)} ~{len(self.updated)} -{len(self.removed)}"


@dataclass(frozen=True)
class BatchResult:
    state: CanvasState | None
    changed: Changed
    errors: tuple[LangError, ...]
    warnings: tuple[str, ...]


# --- binding a statement's arguments to an element -------------------------------------


@dataclass(frozen=True)
class Problem:
    message: str
    expected: str | None = None


@dataclass
class Bound:
    """The result of binding: the element, or the problems that stopped it."""

    element: dict[str, Any] | None
    problems: list[Problem]
    html_supplied: bool


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _needs(spec: ElementSpec, param: Param) -> str:
    suffix = " as a list" if param.kind in ("children", "options") else ""
    return f"{spec.type} needs {param.name}{suffix}"


def name_arguments(
    spec: ElementSpec, args: Sequence[Any], kwargs: Sequence[tuple[str, Any]]
) -> tuple[dict[str, Any], list[Problem]]:
    """Give positional arguments their names; report too many, unknown and doubled ones."""
    sig = signature(spec.type)
    problems: list[Problem] = []
    given: dict[str, Any] = {}
    params = spec.params
    if len(args) > len(params):
        if params:
            problems.append(Problem(f"{spec.type} takes at most {_plural(len(params), 'argument')}; got {len(args)}", sig))
        else:
            problems.append(Problem(f"{spec.type} takes no arguments; got {len(args)}", sig))
    for param, value in zip(params, args):
        given[param.name] = value
    for name, value in kwargs:
        if param_for(spec, name) is None:
            problems.append(Problem(f"{spec.type} has no property '{clip(name)}'", property_help(spec.type)))
        elif name in given:
            problems.append(Problem(f"'{name}' is given twice", sig))
        else:
            given[name] = value
    return given, problems


def _store(param: Param, value: Any) -> Any:
    """The value as the stored form keeps it: plain lists, never the statement's own objects."""
    if param.kind == "options":
        return [list(entry) if isinstance(entry, (list, tuple)) else entry for entry in value]
    if param.kind == "value" and isinstance(value, (list, tuple)):
        return list(value)
    return value


def _choices(props: Mapping[str, Any]) -> list[str]:
    return [o if isinstance(o, str) else o[0] for o in props["options"]]


def _shown(value: Any) -> str:
    """An agent's value as a message shows it: a reference as its plain id, and never more than a few characters."""
    return clip(value.id if isinstance(value, Ref) else str(value))


def check_select_value(eid: str, props: Mapping[str, Any], value: Any) -> str | None:
    """Whether ``value`` fits a Select's options and its ``multiple`` flag."""
    choices = _choices(props)
    if props.get("multiple"):
        if not isinstance(value, (list, tuple)):
            return f"'{eid}' is multiple: its value must be a list of options"
        for item in value:
            if item not in choices:
                return f"'{_shown(item)}' is not one of the options of '{eid}'"
        if len(set(value)) != len(value):
            return f"'{eid}' lists an option twice"
        return None
    if isinstance(value, (list, tuple)):
        return f"'{eid}' takes a single option, not a list"
    if value not in choices:
        return f"'{_shown(value)}' is not one of the options of '{eid}'"
    return None


def _in_option_order(props: Mapping[str, Any], value: Sequence[str]) -> list[str]:
    chosen = set(value)
    return [c for c in _choices(props) if c in chosen]


def _current_argument(current: Mapping[str, Any], name: str) -> Any:
    if name == "children":
        return [Ref(c) for c in current.get("children", [])]
    return copy.deepcopy(current["props"].get(name))


def bind_element(
    spec: ElementSpec,
    args: Sequence[Any],
    kwargs: Sequence[tuple[str, Any]],
    *,
    element_id: str,
    current: Mapping[str, Any] | None,
    max_html_bytes: int,
) -> Bound:
    """Turn ``Type(args)`` into the stored element, or say everything that is wrong with it.

    ``current`` is the element being replaced (for ``...``), if there is one. Every
    problem found is returned, not just the first; the stored form keeps only the
    properties that differ from their defaults.
    """
    sig = signature(spec.type)
    given, problems = name_arguments(spec, args, kwargs)
    kept: set[str] = set()
    failed_keep: set[str] = set()
    for name in [n for n, v in given.items() if v is KEEP]:
        if current is None:
            reason = f"... keeps the current value, but '{element_id}' does not exist yet"
        elif current["type"] != spec.type:
            reason = f"... keeps the current value, but '{element_id}' is of type {current['type']}, not {spec.type}"
        else:
            given[name] = _current_argument(current, name)
            kept.add(name)
            continue
        if not failed_keep:
            problems.append(Problem(reason))
        failed_keep.add(name)
        del given[name]
    props: dict[str, Any] = {}
    children: list[str] | None = None
    for param in all_params(spec):
        value = given.get(param.name)
        if value is None:
            if param.required and param.name not in failed_keep:
                problems.append(Problem(_needs(spec, param), sig))
            continue
        message = validate_arg(spec.type, param, value, max_html_bytes=max_html_bytes)
        if message is not None:
            problems.append(Problem(f"{element_id}: {message}") if param.kind == "html" else Problem(message, sig))
            continue
        if param.kind == "children":
            children = [ref.id for ref in value]
            continue
        stored = _store(param, value)
        if param.default is not None and json_equal(stored, param.default):
            continue
        props[param.name] = copy.deepcopy(stored)
    if spec.type == "Select" and "options" in props and "value" in props:
        message = check_select_value(element_id, props, props["value"])
        if message is not None:
            problems.append(Problem(message))
        elif props.get("multiple"):
            props["value"] = _in_option_order(props, props["value"])
    html_supplied = "html" in given and "html" not in kept
    if problems:
        return Bound(None, problems, html_supplied)
    element: dict[str, Any] = {"type": spec.type, "props": props}
    if spec.container:
        element["children"] = children if children is not None else []
    return Bound(element, [], html_supplied)


def normalize_assign(
    spec: ElementSpec, args: Sequence[Any], kwargs: Sequence[tuple[str, Any]]
) -> tuple[dict[str, Any], list[str] | None]:
    """Name an ``Assign``'s arguments the way the stored form keeps them, without judging them.

    Returns ``(props, children)``; ``...`` and ``null`` arguments are left out and
    defaults are dropped. Raises ValueError for a statement that cannot be named.
    """
    given, problems = name_arguments(spec, args, kwargs)
    if problems:
        raise ValueError(problems[0].message)
    props: dict[str, Any] = {}
    children: list[str] | None = None
    for param in all_params(spec):
        value = given.get(param.name)
        if value is None or value is KEEP:
            continue
        if param.kind == "children":
            children = [v.id if isinstance(v, Ref) else v for v in value]
            continue
        stored = _store(param, value)
        if param.default is not None and json_equal(stored, param.default):
            continue
        props[param.name] = copy.deepcopy(stored)
    return props, children


def _json_shape(value: Any, depth: int) -> str | None:
    """The first way ``value`` is not JSON that the language can write and read back, or None.

    ``depth`` counts the lists and objects around ``value``. The limits are the parser's own
    (``MAX_INT`` and ``MAX_DEPTH``): a value past one of them could be stored, but the program
    ``canvas_get`` printed for it would not parse. A float stops at the integers' magnitude
    (``MAX_FLOAT``), because a database that keeps numbers as numerics gives a float of 1e16 or more
    back as an int.
    """
    if value is None or isinstance(value, (bool, str)):
        return None
    if isinstance(value, int):
        # Compared, never converted: str() and float() both raise for an integer that is long enough.
        return None if -MAX_INT <= value <= MAX_INT else MSG_NUMBER_RANGE
    if isinstance(value, float):
        if not math.isfinite(value):
            return "value must be JSON: no NaN or infinity"
        # The int limit's twin: Postgres JSONB reads a float of magnitude 1e16 or more back as an int, so the round trip of a stored value
        # would not be total. 1e15 is refused with it, mirroring the integer limit (MAX_INT is one short of 10**15).
        return None if abs(value) < MAX_FLOAT else MSG_NUMBER_RANGE
    if isinstance(value, Ref):
        return MSG_BARE_ID
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH:
            return MSG_TOO_DEEP
        for item in value:
            problem = _json_shape(item, depth + 1)
            if problem:
                return problem
        return None
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            return MSG_TOO_DEEP
        for key, item in value.items():
            if not isinstance(key, str):
                return "value must be JSON: object keys are strings"
            problem = _json_shape(item, depth + 1)
            if problem:
                return problem
        return None
    return "value must be JSON (text, numbers, true, false, null, lists and objects)"


def check_json_value(value: Any) -> str | None:
    """Why ``value`` cannot be an Html element's value, or None when it can.

    Public because the service judges a user's value the same way. A value that passes is JSON
    that can be stored (no NUL or lone surrogate), written by ``format_value`` and read back by
    ``parse_program``: integers of at most ``MAX_INT_DIGITS`` digits, finite floats of magnitude below
    ``MAX_FLOAT`` (1e15), lists and objects nested at most ``MAX_DEPTH`` deep, no bare id.
    ``json.dumps`` can measure it too.
    """
    problem = _json_shape(value, 0)
    if problem is None and has_unstorable_char(value):
        return "value contains a character that cannot be stored (a NUL or a lone surrogate)"
    return problem


def _surviving_value(old: Mapping[str, Any], new: Mapping[str, Any], value: Any) -> Any:
    """The user's value after ``old`` is replaced by ``new``, or ``_GONE`` when it no longer fits."""
    if old["type"] != new["type"]:
        return _GONE
    kind = new["type"]
    if kind == "Select":
        if check_select_value("", new["props"], value) is not None:
            return _GONE
        return _in_option_order(new["props"], value) if new["props"].get("multiple") else value
    if kind == "Html":
        return value
    if kind == "Audio":
        return value if old["props"].get("src") == new["props"].get("src") else _GONE
    return _GONE


# --- applying a batch --------------------------------------------------------------------


class _Batch:
    """One run of ``apply_batch`` over a working copy of the state."""

    def __init__(
        self,
        state: CanvasState,
        limits: Limits,
        unit: Literal["line", "op"],
        script_check: Callable[[str], list[str]] | None,
    ) -> None:
        self.before = state
        self.w = state.copy()
        self.limits = limits
        self.unit = unit
        self.script_check = script_check
        self.errors: list[LangError] = []
        self.dropped = 0
        self.dropped_line = 0
        self.warnings: list[str] = []
        self.where: dict[str, int] = {}  # id -> line that last defined it or took it out of a parent
        self.children_line: dict[str, int] = {}  # container id -> line that last set its children
        self.html_line: dict[str, int] = {}  # Html element id -> line that last supplied its html body
        self.last_line = 0
        self.element_ceiling = max(limits.max_elements, state.element_count())
        self.over_since: int | None = None

    # reporting

    def err(self, line: int, message: str, expected: str | None = None) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(LangError(line, message, expected, self.unit))
        else:
            if not self.dropped:
                self.dropped_line = line
            self.dropped += 1

    def error_tuple(self) -> tuple[LangError, ...]:
        errors = list(self.errors)
        if self.dropped:
            errors.append(LangError(self.dropped_line, f"{self.dropped} more errors are not shown", None, self.unit))
        return tuple(errors)

    def _check_ids(self, line: int, *ids: str) -> bool:
        ok = True
        for eid in ids:
            problem = id_problem(eid)
            if problem:
                self.err(line, problem)
                ok = False
        return ok

    # statements

    def apply(self, st: Statement) -> None:
        if not isinstance(st, (Assign, SetProp, Append, Delete, Clear)):
            raise TypeError(f"not a Canvas Lang statement: {st!r}")
        self.last_line = st.line
        if isinstance(st, Assign):
            self._assign(st)
        elif isinstance(st, SetProp):
            self._set_prop(st)
        elif isinstance(st, Append):
            self._append(st)
        elif isinstance(st, Delete):
            self._delete(st)
        else:
            self._clear()
        if self.w.element_count() > self.element_ceiling:
            if self.over_since is None:
                self.over_since = st.line
        else:
            self.over_since = None

    def _assign(self, st: Assign) -> None:
        line = st.line
        if not self._check_ids(line, st.id):
            return
        spec = CATALOG.get(st.type)
        if spec is None:
            self.err(line, f"unknown element type '{clip(st.type)}'", ", ".join(CATALOG))
            return
        if st.id == ROOT_ID and st.type != "Column":
            self.err(line, "root is always a Column")
            return
        current = self.w.elements.get(st.id)
        bound = bind_element(
            spec, st.args, st.kwargs, element_id=st.id, current=current, max_html_bytes=self.limits.max_html_bytes
        )
        problems = list(bound.problems)
        if problems:
            for problem in problems:
                self.err(line, problem.message, problem.expected)
            return
        self._install(st.id, bound.element, current, line)
        if spec.type == "Html":
            if bound.html_supplied:
                self.html_line[st.id] = line
        else:
            self.html_line.pop(st.id, None)

    def _install(self, eid: str, element: dict[str, Any], current: dict[str, Any] | None, line: int) -> None:
        w = self.w
        if eid in w.values:
            kept = _GONE
            if current is not None and w.value_set_by.get(eid) == "user":
                kept = _surviving_value(current, element, w.values[eid])
            if kept is _GONE:
                self._drop_value(eid)
            else:
                w.values[eid] = kept
        w.elements[eid] = element
        self.where[eid] = line
        new_children = element.get("children", [])
        if "children" in element:
            self.children_line[eid] = line
        for child in (current or {}).get("children", []):
            if child not in new_children:
                self.where[child] = line

    def _drop_value(self, eid: str) -> None:
        if self.w.value_set_by.get(eid) == "user":
            self.warnings.append(_RESET_WARNING.format(id=eid))
        self.w.values.pop(eid, None)
        self.w.value_set_by.pop(eid, None)

    def _set_prop(self, st: SetProp) -> None:
        line = st.line
        if not self._check_ids(line, st.id):
            return
        element = self.w.elements.get(st.id)
        if element is None:
            self.err(line, f"no element named '{st.id}'")
            return
        spec = CATALOG[element["type"]]
        if st.prop == "value":
            self._set_value(st, element, spec)
            return
        if st.prop == "children":
            self._set_children(st, element, spec)
            return
        param = param_for(spec, st.prop)
        if param is None:
            self.err(line, f"{spec.type} has no property '{clip(st.prop)}'", property_help(spec.type))
            return
        value = st.value
        if value is KEEP:
            return
        sig = signature(spec.type)
        props = element["props"]
        if value is None:
            if param.required:
                self.err(line, _needs(spec, param), sig)
                return
            props.pop(param.name, None)
        else:
            message = validate_arg(spec.type, param, value, max_html_bytes=self.limits.max_html_bytes)
            if message is not None:
                if param.kind == "html":
                    self.err(line, f"{st.id}: {message}")
                else:
                    self.err(line, message, sig)
                return
            if param.kind == "html":
                self.html_line[st.id] = line
            stored = copy.deepcopy(_store(param, value))
            if param.default is not None and json_equal(stored, param.default):
                props.pop(param.name, None)
            else:
                props[param.name] = stored
        if spec.type == "Select" and param.name in ("options", "multiple", "value"):
            self._recheck_select(st.id, element, line)

    def _recheck_select(self, eid: str, element: dict[str, Any], line: int) -> None:
        props = element["props"]
        if "value" in props:
            message = check_select_value(eid, props, props["value"])
            if message is not None:
                self.err(line, message)
                return
            if props.get("multiple"):
                props["value"] = _in_option_order(props, props["value"])
        if eid in self.w.values:
            live = self.w.values[eid]
            if check_select_value(eid, props, live) is not None:
                self._drop_value(eid)
            elif props.get("multiple"):
                self.w.values[eid] = _in_option_order(props, live)

    def _set_value(self, st: SetProp, element: dict[str, Any], spec: ElementSpec) -> None:
        line, eid, value = st.line, st.id, st.value
        if spec.type not in ("Select", "Html"):
            self.err(line, f"{spec.type} has no value")
            return
        if value is KEEP:
            return
        if value is not None:
            if spec.type == "Select":
                message = check_select_value(eid, element["props"], value)
                if message is not None:
                    self.err(line, message)
                    return
                if element["props"].get("multiple"):
                    value = _in_option_order(element["props"], value)
            else:
                message = check_json_value(value)
                if message is not None:
                    self.err(line, message)
                    return
                try:
                    size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
                except ValueError:  # check_json_value bounded what json.dumps can meet; never a crash if it is wrong
                    self.err(line, "value must be JSON (text, numbers, true, false, null, lists and objects)")
                    return
                if size > HTML_VALUE_MAX_CHARS:
                    self.err(line, f"Html value is {size} characters of JSON; the limit is {HTML_VALUE_MAX_CHARS}")
                    return
        w = self.w
        if value is None:
            w.values.pop(eid, None)
            w.value_set_by.pop(eid, None)
            return
        if json_equal(value, w.current_value(eid)):
            return
        w.values[eid] = copy.deepcopy(value)
        w.value_set_by[eid] = "agent"

    def _set_children(self, st: SetProp, element: dict[str, Any], spec: ElementSpec) -> None:
        line = st.line
        if not spec.container:
            self.err(line, f"{spec.type} has no property 'children'", property_help(spec.type))
            return
        value = st.value
        if value is KEEP:
            return
        sig = signature(spec.type)
        param = spec.params[0]
        if value is None:
            self.err(line, _needs(spec, param), sig)
            return
        message = validate_arg(spec.type, param, value, max_html_bytes=self.limits.max_html_bytes)
        if message is not None:
            self.err(line, message, sig)
            return
        new_children = [ref.id for ref in value]
        for old in element["children"]:
            if old not in new_children:
                self.where[old] = line
        element["children"] = new_children
        self.children_line[st.id] = line

    def _append(self, st: Append) -> None:
        line = st.line
        if not self._check_ids(line, st.id, *st.children):
            return
        element = self.w.elements.get(st.id)
        if element is None:
            self.err(line, f"no element named '{st.id}'")
            return
        if "children" not in element:
            self.err(line, f"{element['type']} cannot have children; only Column, Row and Card can")
            return
        element["children"].extend(st.children)
        self.children_line[st.id] = line

    def _delete(self, st: Delete) -> None:
        for eid in st.ids:
            if not self._check_ids(st.line, eid):
                continue
            if eid == ROOT_ID:
                self.err(st.line, "root cannot be deleted; use clear to empty the canvas")
            elif eid not in self.w.elements:
                self.err(st.line, f"no element named '{eid}'")
            else:
                self._remove_subtree(eid)

    def _remove_subtree(self, eid: str) -> None:
        w = self.w
        doomed: list[str] = []
        seen = {eid}
        stack = [eid]
        while stack:
            current = stack.pop()
            doomed.append(current)
            for child in w.elements[current].get("children", ()):
                if child in w.elements and child not in seen:
                    seen.add(child)
                    stack.append(child)
        for element in w.elements.values():
            if "children" in element and eid in element["children"]:
                element["children"] = [c for c in element["children"] if c != eid]
        for gone in doomed:
            w.elements.pop(gone, None)
            w.values.pop(gone, None)
            w.value_set_by.pop(gone, None)
            self.where.pop(gone, None)
            self.children_line.pop(gone, None)
            self.html_line.pop(gone, None)

    def _clear(self) -> None:
        self.w.elements = {ROOT_ID: copy.deepcopy(ROOT_ELEMENT)}
        self.w.values = {}
        self.w.value_set_by = {}
        self.where.clear()
        self.children_line.clear()
        self.html_line.clear()

    # the whole canvas, judged once every statement has been applied

    def _line_of_children(self, eid: str) -> int:
        return self.children_line.get(eid) or self.where.get(eid) or self.last_line

    def finish(self) -> None:
        w = self.w
        found: list[tuple[int, str]] = []
        containers = [(eid, el) for eid, el in w.elements.items() if "children" in el]
        known = w.elements

        for eid, el in containers:
            reported: set[str] = set()
            for child in el["children"]:
                if child not in known and child not in reported:
                    reported.add(child)
                    found.append((self._line_of_children(eid), f"unknown element '{child}' in the children of '{eid}'"))

        back_edges = self._find_cycles(found)

        claims: dict[str, list[str]] = {}
        for eid, el in containers:
            for child in el["children"]:
                if child in known and (eid, child) not in back_edges:
                    claims.setdefault(child, []).append(eid)
        for child, parents in claims.items():
            if child == ROOT_ID:
                found.append((self._line_of_children(parents[-1]), "'root' cannot be inside another element"))
            elif len(parents) > 1:
                order = sorted(range(len(parents)), key=lambda i: (self.children_line.get(parents[i], 0), i))
                first = parents[order[0]]
                for i in order[1:]:
                    found.append((self._line_of_children(parents[i]), f"'{child}' is already a child of '{first}'"))

        reachable: set[str] = set()
        stack = [ROOT_ID]
        while stack:
            current = stack.pop()
            if current in reachable or current not in known:
                continue
            reachable.add(current)
            stack.extend(known[current].get("children", ()))
        for eid in known:
            if eid not in reachable and eid not in claims:
                found.append((self.where.get(eid, self.last_line), f"'{eid}' is not reachable from root; add it to a container's children"))

        count = w.element_count()
        if count > self.element_ceiling:
            line = self.over_since or self.last_line
            found.append((line, f"the batch would make {count} elements; the limit is {self.limits.max_elements}"))
        size = w.size_bytes()
        if size > max(self.limits.max_canvas_bytes, self.before.size_bytes()):
            found.append((self.last_line, f"the canvas would be {size} bytes; the limit is {self.limits.max_canvas_bytes}"))

        for line, message in sorted(found, key=lambda item: item[0]):
            self.err(line, message)
        if not self.errors and self.script_check is not None and self.html_line:
            surviving = [
                (eid, line, w.elements[eid]["props"]["html"])
                for eid, line in sorted(self.html_line.items(), key=lambda item: item[1])
                if eid in w.elements and w.elements[eid].get("type") == "Html"
            ]
            distinct = list(dict.fromkeys(html for _, _, html in surviving))
            if distinct:
                many = getattr(self.script_check, "many", None)
                by_body = many(distinct) if callable(many) else {html: self.script_check(html) for html in distinct}
                for eid, line, html in surviving:
                    for text in by_body.get(html, ()):
                        self.err(line, f"{eid}: {text}")

    def _find_cycles(self, found: list[tuple[int, str]]) -> set[tuple[str, str]]:
        """Depth-first search for containers that end up inside themselves.

        Reports each closing edge once and returns those edges, so they are not
        also counted as a second parent.
        """
        known = self.w.elements
        color: dict[str, int] = {}
        back_edges: set[tuple[str, str]] = set()
        for start in known:
            if color.get(start):
                continue
            color[start] = 1
            stack = [(start, iter(known[start].get("children", ())))]
            while stack:
                node, children = stack[-1]
                for child in children:
                    if child not in known:
                        continue
                    state = color.get(child, 0)
                    if state == 1:
                        if (node, child) not in back_edges:
                            back_edges.add((node, child))
                            found.append((self._line_of_children(node), f"'{child}' would contain itself"))
                    elif state == 0:
                        color[child] = 1
                        stack.append((child, iter(known[child].get("children", ()))))
                        break
                else:
                    color[node] = 2
                    stack.pop()
        return back_edges


def _diff(before: CanvasState, after: CanvasState) -> Changed:
    added = tuple(eid for eid in after.elements if eid not in before.elements)
    removed = tuple(eid for eid in before.elements if eid not in after.elements)
    updated: list[str] = []
    for eid, element in after.elements.items():
        if eid not in before.elements:
            continue
        if element != before.elements[eid] or not json_equal(before.current_value(eid), after.current_value(eid)):
            updated.append(eid)
    return Changed(added, tuple(updated), removed)


def apply_batch(
    state: CanvasState,
    statements: Sequence[Statement],
    *,
    limits: Limits,
    unit: Literal["line", "op"] = "line",
    script_check: Callable[[str], list[str]] | None = None,
) -> BatchResult:
    """Apply ``statements`` to ``state`` as one all-or-nothing batch.

    Statements apply in order to a working copy; children references are resolved
    at the end, so a container may name an element defined later. When any
    statement is wrong the result carries every error and ``state=None``; the
    whole-canvas rules (reachability, one parent, cycles, limits) are judged only
    for a batch whose statements were all sound, so one bad line does not drown
    the agent in follow-on errors. ``state`` is never mutated and ``rev`` is left
    to the caller. ``script_check(html)`` returns problems with an Html body's
    scripts; each becomes an error on the Html statement's line.
    """
    batch = _Batch(state, limits, unit, script_check)
    for statement in statements:
        batch.apply(statement)
    if not batch.errors:
        batch.finish()
    if batch.errors:
        return BatchResult(None, Changed(), batch.error_tuple(), ())
    work = batch.w
    work.values = {k: v for k, v in work.values.items() if k in work.elements}
    work.value_set_by = {k: v for k, v in work.value_set_by.items() if k in work.elements}
    return BatchResult(work, _diff(state, work), (), tuple(batch.warnings))


__all__ = [
    "ROOT_ELEMENT",
    "BatchResult",
    "Bound",
    "CanvasState",
    "Changed",
    "Limits",
    "Problem",
    "apply_batch",
    "bind_element",
    "check_json_value",
    "check_select_value",
    "json_equal",
    "name_arguments",
    "normalize_assign",
]
