"""Decompile a canvas into the Canvas Lang program ``canvas_get`` returns (Pivot P5, Task 3).

The program recreates the canvas: elements in pre-order from the root, the
user's choices as ordinary ``id.value = ...`` lines marked ``# user``, long Html
bodies elided unless asked for, and the events since the agent's last read as
comments. The agent can copy a line, edit it and send it back (research report
d.5). Pure: the clock is a parameter.

How an element is written (Decision 5): required and non-boolean optional
arguments are positional until the first one that is left out; booleans, and
everything after a gap, are ``name=value``; defaults are left out (the stored
form never holds them).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from primer.canvas.catalog import CATALOG, COMMON_PROPS
from primer.canvas.lang import ROOT_ID, Ref, format_value
from primer.canvas.state import CanvasState, json_equal

#: Events listed under the program; the rest are counted.
MAX_EVENT_LINES = 20

_VALUE_ELEMENTS = ("Select", "Html")


def _readable(value: Any) -> bool:
    """Whether ``value`` is a number a playback position can be: not a boolean, NaN or infinite.

    math.isfinite() raises OverflowError for an int too big for a float, and no position is that long, so
    such an int is as unreadable as NaN.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _seconds(value: Any) -> str:
    """Seconds as the event lines print them: whole numbers bare, else one decimal; anything unreadable is 0."""
    if not _readable(value):
        return "0"
    text = f"{round(float(value), 1):.1f}"
    return text[:-2] if text.endswith(".0") else text


def _clock(value: Any) -> str:
    """``m:ss`` for a playback position; anything unreadable is 0:00."""
    total = int(value) if _readable(value) and value >= 0 else 0
    return f"{total // 60}:{total % 60:02d}"


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _age(at: Any, now: datetime | None) -> str | None:
    moment = _moment(at)
    if moment is None or now is None:
        return None
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - moment).total_seconds()))
    return f"{seconds}s ago" if seconds < 60 else f"{seconds // 60}m ago"


def event_line(event: Mapping[str, Any], *, now: datetime | None = None) -> str:
    """One event as a line: ``e17 genre change value="Lo-fi" by="alice" 12s ago``."""
    kind = event.get("kind")
    parts = [str(event.get("id", "")), str(event.get("element", "")), str(kind)]
    if kind in ("change", "set_value") and "value" in event:
        parts.append(f"value={format_value(event['value'])}")
    elif kind == "emit":
        parts.append(f"name={format_value(str(event.get('name', '')))}")
        if event.get("data") is not None:
            parts.append("data=" + json.dumps(event["data"], separators=(",", ":"), ensure_ascii=False))
    elif kind in ("play", "pause", "ended") and event.get("position") is not None:
        parts.append(f"position={_seconds(event['position'])}")
    if event.get("by"):
        parts.append(f"by={format_value(str(event['by']))}")
    age = _age(event.get("at"), now)
    if age:
        parts.append(age)
    return " ".join(parts)


def _html_literal(body: str) -> str:
    """A full Html body: verbatim in triple quotes when that reads back exactly, else a JSON string."""
    plain = all(c in "\n\t" or ord(c) >= 0x20 for c in body)
    if plain and '"""' not in body and "\r" not in body and any(c in body for c in '\n"\\'):
        return '"""' + body + '"""'
    return format_value(body)


def _audio_note(value: Any) -> str | None:
    if not isinstance(value, Mapping) or not ({"playing", "position", "ended"} & set(value)):
        return None
    if value.get("ended"):
        return "user: ended"
    return f"user: {'playing' if value.get('playing') else 'paused'} at {_clock(value.get('position'))}"


def _arguments(element: Mapping[str, Any], *, full: bool) -> list[str]:
    spec = CATALOG[element["type"]]
    props = element["props"]
    out: list[str] = []
    positional = True
    for param in spec.params:
        if param.kind == "children":
            text = format_value([Ref(c) for c in element.get("children", [])])
        elif param.name in props:
            value = props[param.name]
            if param.kind == "html":
                text = _html_literal(value) if full else "..."
            else:
                text = format_value(value)
        else:
            positional = False
            continue
        if positional and param.kind != "bool":
            out.append(text)
        else:
            positional = False
            out.append(f"{param.name}={text}")
    own = {p.name for p in spec.params}
    for name in COMMON_PROPS:
        if name not in own and name in props:
            out.append(f"{name}={format_value(props[name])}")
    return out


def _element_lines(eid: str, state: CanvasState, *, full: bool) -> list[str]:
    element = state.elements[eid]
    kind = element["type"]
    line = f"{eid} = {kind}({', '.join(_arguments(element, full=full))})"
    note: str | None = None
    if kind == "Html" and not full:
        note = f"{len(element['props'].get('html', ''))} chars; canvas_get full=true shows the body"
    elif kind == "Audio":
        note = _audio_note(state.values.get(eid))
    lines = [line + (f"  # {note}" if note else "")]
    if kind in _VALUE_ELEMENTS and eid in state.values:
        current = state.values[eid]
        if not json_equal(current, element["props"].get("value")):
            suffix = "  # user" if state.value_set_by.get(eid) == "user" else ""
            lines.append(f"{eid}.value = {format_value(current)}{suffix}")
    return lines


def _preorder(state: CanvasState, starts: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    order: list[str] = []
    for start in starts:
        stack = [start]
        while stack:
            eid = stack.pop()
            if eid in seen or eid not in state.elements:
                continue
            seen.add(eid)
            order.append(eid)
            stack.extend(reversed(state.elements[eid].get("children", [])))
    return order


def decompile(
    state: CanvasState,
    *,
    ids: Sequence[str] | None = None,
    full: bool = False,
    events: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> str:
    """The program for ``state``; ``ids`` limits it to those elements and their subtrees.

    ``full`` writes Html bodies out; without it they are ``...``, which a later
    batch reads as "keep what is there". ``events`` are listed under the program
    (at most 20, then counted), with their age when ``now`` is given.
    """
    if state.element_count() == 0:
        lines = [f"# canvas rev {state.rev}: empty"]
    else:
        lines = [f'# canvas rev {state.rev}; lines marked "# user" were set by the user']
    starts = list(ids) if ids else [ROOT_ID]
    if ids:
        lines.append("# part of the canvas: " + ", ".join(ids))
        lines += [f"# no element named '{eid}'" for eid in ids if eid not in state.elements]
    if state.element_count() or ids:
        for eid in _preorder(state, starts):
            lines += _element_lines(eid, state, full=full)
    if events:
        lines.append(f"# events since your last read ({len(events)}):")
        lines += [f"#   {event_line(event, now=now)}" for event in events[:MAX_EVENT_LINES]]
        if len(events) > MAX_EVENT_LINES:
            lines.append(f"#   ... and {len(events) - MAX_EVENT_LINES} more")
    return "\n".join(lines) + "\n"


__all__ = ["MAX_EVENT_LINES", "decompile", "event_line"]
