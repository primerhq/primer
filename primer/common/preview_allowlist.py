"""The allowlist an approval prompt may draw from (design note 01a11cd3-66b0).

Which arguments an approval card may show is declared as DOTTED PATHS by the tool (``Tool.preview_args``) or by the operator's
approval policy (``ToolApprovalPolicy.preview_args``); the PATH LANGUAGE (syntax, schema checks, closed sets, ``classify``) is
:mod:`primer.common.preview_paths`. This module is where that language is APPLIED: the stamp a park writes into
``resume_metadata["preview"]``, the walk that withholds every argument the allowlist does not name, the default rule for a park
that stamped nothing (design ruling D5), and :func:`scrub_args_for_display`, the single place BOTH the console pending-yields
route and the channel approval prompt (01a125ed-9ef6: the channel used to post the raw arguments) ask "what may this prompt
show?".

Everything withheld is replaced with ``<hidden>``. That word is DIFFERENT from the scrubber's ``<redacted>``: ``<redacted>`` says
the scrubber FOUND a secret in a value it read; ``<hidden>`` says the value was never read at all. A withheld value is replaced
WITHOUT being looked at: it is not stringified, measured, iterated or compared, so it cannot leak through anything this module
does.
"""

from __future__ import annotations

import json
from typing import Any

from primer.common.preview_paths import MAX_PATHS, classify, is_segment, path_syntax_error

# What an approval card draws for an argument the allowlist withholds. Its word is DIFFERENT from the scrubber's ``<redacted>``
# (a value the scrubber read and masked): ``<hidden>`` says the value was never read at all (design note 01a11cd3-66b0).
_HIDDEN = "<hidden>"
_PREVIEW_SOURCES = frozenset({"policy", "tool", "default"})
_HIDDEN_PATHS_KEPT = 64

# A name/value pair written as {"name": "Authorization", "value": "..."}: the value is secret when the name is.
_PAIR_NAME_KEYS = frozenset({"name", "key", "header", "field", "param", "variable", "var", "env", "label"})
_PAIR_VALUE_KEYS = frozenset({"value", "val"})
# The walker (the scrubber's and the allowlist's) looks no deeper than this, at no more members than this, and at no more than
# _REDACT_BUDGET characters (a member costs _REDACT_MEMBER_COST) in one walk; what it does not look at is hidden, and a
# container it cut says how many it left out. It runs synchronously on the event loop for every poll of the pending-yields route
# and every post-park channel dispatch, so its work has to be bounded by constants, not by the size of whatever a tool call or a
# question happened to contain.
_REDACT_MAX_DEPTH = 8
_REDACT_MAX_ITEMS = 50
_REDACT_BUDGET = 12000
_REDACT_MEMBER_COST = 16
_NAME_SCAN_CHARS = 200             # an argument name is scanned no further than this
_BIG_DOCUMENT_CHARS = 1_000_000    # a JSON string bigger than this is text, not a document to parse
# A name the DEFAULT rule allows (a closed set by its schema: boolean, number, null, enum, const) is drawn when its value is also of that kind: the gate parks BEFORE schema
# validation, so a model can send any text under such a name, and a secret sent as an "enum" must not ride on the schema's promise.
_DEFAULT_TEXT_CHARS = 40


def _parse_container(text: str) -> "dict | list | None":
    """The dict or list ``text`` is a JSON document of, else ``None`` (deeply nested text is not a document, it is a hazard)."""
    if text.lstrip()[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _preview_stamp(metadata: Any) -> dict[str, Any] | None:
    """The allowlist the park stamped into ``resume_metadata["preview"]`` (``{"paths": [...], "source": "policy" | "tool" | "default"}``), or ``None`` when the
    row has no usable stamp (parked before the field existed, by a site that does not stamp, or a malformed one): the caller then applies the default rule."""
    stamp = metadata.get("preview") if isinstance(metadata, dict) else None
    if not isinstance(stamp, dict):
        return None
    paths, source = stamp.get("paths"), stamp.get("source")
    if source not in _PREVIEW_SOURCES or not isinstance(paths, list) or len(paths) > MAX_PATHS:
        return None
    if any(path_syntax_error(path) is not None for path in paths):
        return None
    return {"paths": list(paths), "source": source}


def _allow_only(args: dict, paths: "list[str]", budget: "list[int] | None" = None) -> tuple[dict, list[str]]:
    """``args`` with every member the allowlist does not name replaced by ``_HIDDEN``, and the dotted paths that were withheld (each once, in the order met).

    A path allows its whole subtree and a list is transparent (``classify``). A container with an allowed path deeper inside is walked member by member; a member
    that is not allowed is replaced WITHOUT being looked at: it is not stringified, measured, iterated or compared, so a withheld value cannot leak through
    anything this function does.

    The walk is bounded by constants, like the scrubber's, and charges the same ``budget`` (the preview's, one element list; a fresh one when not given): every
    position it visits costs ``_REDACT_MEMBER_COST`` plus the length of the path it built, a container is cut at ``_REDACT_MAX_ITEMS`` members, and what the budget
    does not cover is withheld and counted (``<N more>``), never walked. Lists are transparent, so without the charge a list of lists with a path deeper inside
    would be walked in full: ``width ** depth`` visits on the event loop of every poll of the pending-yields route.

    A key that is not one path segment (``entity.id``, ``a b``) is withheld where a path goes deeper: joined with its parents it would read as a nested path and
    show a value the allowlist never named. A name/value pair (``{"name": ..., "value": ...}``) whose name is not shown whole has its value withheld too: the
    name was not read, so it cannot be judged, and a secret name makes the value a secret."""
    hidden: list[str] = []
    if budget is None:
        budget = [_REDACT_BUDGET]

    def withhold(path: str) -> str:
        if len(hidden) < _HIDDEN_PATHS_KEPT and path not in hidden:
            hidden.append(path)
        return _HIDDEN

    def more(path: str, count: int) -> str:
        marker = f"<{count} more>"
        withhold(f"{path} {marker}" if path else marker)
        return marker

    def look(path: str) -> str:
        """``classify`` for one visit, charged to the budget; a position there is no budget left for is not looked at (``none``: withheld)."""
        if budget[0] <= 0:
            return "none"
        budget[0] -= _REDACT_MEMBER_COST + len(path)
        return classify(paths, path)

    def walk(value: Any, path: str, depth: int, kind: str) -> Any:
        if kind == "all":
            return value
        if kind == "none" or depth > _REDACT_MAX_DEPTH:
            return withhold(path)
        if isinstance(value, dict):
            return walk_dict(value, path, depth)
        if isinstance(value, (list, tuple)):
            items: list[Any] = []
            for index, member in enumerate(value):
                if index >= _REDACT_MAX_ITEMS or budget[0] <= 0:
                    items.append(more(path, len(value) - index))
                    break
                budget[0] -= _REDACT_MEMBER_COST + len(path)
                items.append(walk(member, path, depth + 1, kind))          # a list is transparent: its members are at the list's own path
            return items
        return withhold(path)          # a deeper path was allowed but the value is a scalar: there is no part of it to show

    def walk_dict(value: dict, path: str, depth: int) -> dict:
        out: dict[Any, Any] = {}
        kinds: dict[Any, str] = {}
        cut = False
        for index, key in enumerate(value):
            # the top level is always walked (to the item cap): an argument the budget does not cover is still NAMED on the card, as withheld
            if index >= _REDACT_MAX_ITEMS or (budget[0] <= 0 and path):
                out["..."] = more(path, len(value) - index)
                cut = True
                break
            name = str(key)[:_NAME_SCAN_CHARS]
            child = f"{path}.{name}" if path else name
            if not is_segment(key):
                budget[0] -= _REDACT_MEMBER_COST + len(child)
                kinds[key], out[key] = "none", withhold(child)
                continue
            kinds[key] = look(child)
            out[key] = walk(value[key], child, depth + 1, kinds[key])
        if cut or any(kinds.get(key) != "all" for key in out if str(key)[:_NAME_SCAN_CHARS].lower() in _PAIR_NAME_KEYS):
            for key in out:
                if str(key)[:_NAME_SCAN_CHARS].lower() in _PAIR_VALUE_KEYS and out[key] is not _HIDDEN:
                    out[key] = withhold(f"{path}.{key}" if path else str(key))
        return out

    return walk_dict(args, "", 0), hidden


def _value_rule(args: dict) -> tuple[dict, list[str]]:
    """The default rule for a row with NO stamp (design ruling D5), by the VALUE's type since there is no schema to ask: a boolean, a number or ``null`` is shown, text and
    containers are withheld (not looked at), and so is a value beside a withheld pair name (``name``/``value``). ``args`` with the withheld paths, like
    :func:`_allow_only`."""
    hidden: list[str] = []
    out: dict[Any, Any] = {}
    for key, value in args.items():
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        else:
            out[key] = _HIDDEN
            if len(hidden) < _HIDDEN_PATHS_KEPT:
                hidden.append(str(key))
    if any(out[key] is _HIDDEN for key in out if str(key)[:_NAME_SCAN_CHARS].lower() in _PAIR_NAME_KEYS):
        for key in out:
            if str(key)[:_NAME_SCAN_CHARS].lower() in _PAIR_VALUE_KEYS and out[key] is not _HIDDEN:
                out[key] = _HIDDEN
                if len(hidden) < _HIDDEN_PATHS_KEPT:
                    hidden.append(str(key))
    return out, hidden


def _of_the_default_kind(value: Any) -> bool:
    """A boolean, a number, ``null`` or a short string: what a closed-set argument holds when the call is the one its schema describes."""
    return value is None or isinstance(value, (bool, int, float)) or (isinstance(value, str) and len(value) <= _DEFAULT_TEXT_CHARS)


def scrub_args_for_display(args: Any, stamp: dict[str, Any] | None) -> tuple[Any, list[str]]:
    """The arguments an approval prompt may show under ``stamp``, and the dotted paths that were withheld.

    ``args`` is the call's arguments as parked: a dict, a JSON string of a dict or list (parsed first, like the console route), or a bare value. ``stamp`` is the
    validated allowlist the park stamped (:func:`_preview_stamp`), or ``None`` for a park that stamped nothing (the default rule, design ruling D5).

    Returns the value to show - the scrubbed dict (a withheld member drawn as its name and ``<hidden>``, never read), ``<hidden>`` for a bare value no path can
    name, or the empty/None argument as is - and the withheld paths (each once, in the order met). The call's own arguments are not mutated.
    """
    if isinstance(args, str):
        if len(args) <= _BIG_DOCUMENT_CHARS:
            document = _parse_container(args)
            if document is not None:
                args = document
    if isinstance(args, dict):
        if stamp is None:
            return _value_rule(args)
        scrubbed, withheld = _allow_only(args, stamp["paths"], [_REDACT_BUDGET])
        if stamp["source"] == "default":
            for key in list(scrubbed):
                if scrubbed[key] is not _HIDDEN and not _of_the_default_kind(scrubbed[key]):
                    scrubbed[key] = _HIDDEN
                    if len(withheld) < _HIDDEN_PATHS_KEPT and str(key) not in withheld:
                        withheld.append(str(key))
        return scrubbed, withheld
    if args is None or args == "":
        return args, []
    return _HIDDEN, []        # no path can name a bare value: it is withheld whole


__all__ = [
    "_HIDDEN",
    "_PREVIEW_SOURCES",
    "_HIDDEN_PATHS_KEPT",
    "_parse_container",
    "_preview_stamp",
    "_allow_only",
    "_value_rule",
    "_of_the_default_kind",
    "scrub_args_for_display",
]
