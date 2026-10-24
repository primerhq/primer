"""The text a canvas event reaches the agent as (pivot P5, Task 12; design 8.5, plan Decision 9).

Two blocks, each the agent's own input in its own session and nothing else:

* :func:`canvas_event_block` is the instruction a waking event (a Button click, an Html emit) becomes, whether it wakes an idle session
  at once or waits in the session's queue of steers: ``<canvas_event ...>`` with the event's attributes, then what the user did (the
  value, the emit's name and data, the playback position) and the user's current values, then ``</canvas_event>``.
* :func:`canvas_changes_block` is the digest ``wake_session`` adds to an instruction: the silent changes the agent has not read yet,
  one line each (:func:`primer.canvas.decompile.event_line`), at most :data:`MAX_DIGEST_LINES`.

What the user ENTERED (a value, an emit's data) is in these texts and only in these texts (the lead's ruling 1 on S5, the rule of #872):
every structured copy of the event (the queued steer's attribution, the USER_INPUT's ``payload.canvas_event``, the digest's payload, a
``canvas_event`` record) is the event without its ``value`` and ``data`` (``primer.canvas.delivery``).

Nothing a user typed or an agent wrote can close a block or start a line in it: every string is written as a JSON string, so a newline
in it is ``\\n``, and every ``<`` in a line of a block is written ``\\u003c`` (both lines and attributes are made only of ids the canvas
validated, numbers and such strings, so the only ``<`` a block holds are its own two tags). The copy is exact (plan Global Constraints).
Pure: no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from primer.canvas.decompile import _seconds, event_line, preorder
from primer.canvas.lang import ROOT_ID, format_value
from primer.canvas.state import CanvasState
from primer.model.canvas import MAX_EMIT_DATA_BYTES, CanvasEvent

#: The most characters of the ``values:`` line of an event block; past it the rest are counted.
MAX_VALUES_LINE_CHARS = 2000
#: The most events a digest lists one per line; past it the rest are counted (and stay unread: audit G8-07).
MAX_DIGEST_LINES = 10

_TRUNCATED = "...(truncated)"
#: The attributes of the opening tag, in their order; ``label`` only when the event has one.
_ATTRIBUTES = ("id", "rev", "element", "type", "kind", "source", "by")


def _shield(text: str) -> str:
    """``text`` with every ``<`` written ``\\u003c``: a block's lines hold ``<`` only inside strings, where the escape reads back."""
    return text.replace("<", "\\u003c")


def _literal(value: Any) -> str:
    """``value`` as a Canvas Lang literal, or compact JSON for one the literal cannot write (too deep). Not yet shielded."""
    try:
        return format_value(value)
    except (TypeError, ValueError):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _attribute(name: str, value: Any) -> str:
    return f"{name}={_shield(json.dumps(str(value), ensure_ascii=False))}"


def _data_line(data: Any) -> str:
    """``data: <compact JSON>``, cut at :data:`MAX_EMIT_DATA_BYTES` bytes (on a character boundary) with ``...(truncated)``."""
    text = _shield(json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str))
    raw = text.encode("utf-8")
    if len(raw) > MAX_EMIT_DATA_BYTES:
        text = raw[:MAX_EMIT_DATA_BYTES].decode("utf-8", errors="ignore") + _TRUNCATED
    return f"data: {text}"


def _values_line(state: CanvasState) -> str | None:
    """``values: id=<value>, ...``: every element whose current value the user set, in tree order; ``None`` when there is none.

    The line holds at most :data:`MAX_VALUES_LINE_CHARS` characters: the values that do not fit are counted,
    ``, ... ({n} more; call canvas_get)``.
    """
    items = [
        _shield(f"{eid}={_literal(state.values[eid])}")
        for eid in preorder(state, [ROOT_ID])
        if eid in state.values and state.value_set_by.get(eid) == "user"
    ]
    if not items:
        return None
    line = "values: " + ", ".join(items)
    if len(line) <= MAX_VALUES_LINE_CHARS:
        return line
    kept: list[str] = []
    for index, item in enumerate(items):
        rest = len(items) - index - 1
        if len("values: " + ", ".join([*kept, item]) + f", ... ({rest} more; call canvas_get)") > MAX_VALUES_LINE_CHARS:
            break
        kept.append(item)
    more = f"... ({len(items) - len(kept)} more; call canvas_get)"
    return "values: " + (", ".join(kept) + ", " if kept else "") + more


def canvas_event_block(event: CanvasEvent, *, state: CanvasState) -> str:
    """The ``<canvas_event>`` block of ``event``, with the user's values of ``state`` (the canvas after the event)."""
    attributes = [_attribute(name, getattr(event, name)) for name in _ATTRIBUTES]
    if event.label is not None:
        attributes.append(_attribute("label", event.label))
    lines = ["<canvas_event " + " ".join(attributes) + ">"]
    if event.kind in ("change", "set_value"):
        lines.append(_shield(f"value: {_literal(event.value)}"))
    if event.kind == "emit":
        lines.append(_shield(f"name: {json.dumps(str(event.name or ''), ensure_ascii=False)}"))
        if event.data is not None:
            lines.append(_data_line(event.data))
    if event.position is not None:
        lines.append(f"position: {_seconds(event.position)}")
    values = _values_line(state)
    if values is not None:
        lines.append(values)
    lines.append("</canvas_event>")
    return "\n".join(lines)


def canvas_changes_block(events: Sequence[CanvasEvent], *, rev: int) -> str:
    """The ``<canvas_changes>`` digest of ``events`` (oldest first) on a canvas at ``rev``: the first :data:`MAX_DIGEST_LINES` as lines,
    the rest counted."""
    lines = [f'<canvas_changes rev="{int(rev)}" count="{len(events)}">']
    lines += [_shield(event_line(event.public())) for event in events[:MAX_DIGEST_LINES]]
    if len(events) > MAX_DIGEST_LINES:
        lines.append(f"... and {len(events) - MAX_DIGEST_LINES} more (call canvas_get)")
    lines.append("</canvas_changes>")
    return "\n".join(lines)


__all__ = ["MAX_DIGEST_LINES", "MAX_VALUES_LINE_CHARS", "canvas_changes_block", "canvas_event_block"]
