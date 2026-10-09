"""A ratchet on the graph builder's clickable things that are not controls (board task 01a12124).

A ``<span onClick>`` or ``<div onClick>`` with no ``role`` is invisible to a keyboard and a screen reader: no focus, no key, no name. The builder had 21 of them; the three the #692 review found
(+ Add a path, Remove this connection, Delete this step) and the Advanced toggle in front of one of them are buttons now, and this file counts what is left, per file. The counts may only go DOWN:
a new one fails, and a file that got below its count fails until the baseline is lowered (so the number is never a cushion for a later one). The rest are listed in ``BASELINE`` for the follow-up that
turns each into a button, a ``role="button"`` with a key handler, or a link.

What is counted: an opening ``<span>``, ``<div>``, ``<a>``, ``<li>``, ``<p>``, ``<td>``, ``<tr>``, ``<section>`` or ``<label>`` whose attributes hold ``onClick=`` and no ``role=``. (A ``<label>`` is clickable by
nature when it wraps a control, but one with its own ``onClick`` is not a control; none is in the builder today.)
"""

from __future__ import annotations

import re
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[2] / "ui" / "components" / "graph-builder"

# the clickable non-controls that are still there, per file (can only shrink)
BASELINE = {
    "gb-dryrun.jsx": 2,
    "gb-inspector.jsx": 2,       # the template-kind cards, the fan-out spec chips
    "gb-outline.jsx": 1,         # the step rows
    "gb-palette.jsx": 4,         # the scrim, a purpose row, Back, a tool row
    "gb-readiness.jsx": 1,       # the readiness chip
    "gb-ref-editor.jsx": 2,      # the "/" hint, a value row
    "gb-schema.jsx": 4,          # the JSON toggles, required, remove a field
    "gb-starters.jsx": 1,        # a starter card
    "graph-builder.jsx": 1,      # the bands toggle
}

TAGS = re.compile(r"<(span|div|a|li|p|td|tr|section|label)\b")


def opening_tag(text: str, start: int) -> str:
    """The opening tag that begins at ``start``: up to the first ``>`` outside braces and quotes (an arrow function in ``onClick={() => ...}`` is inside braces)."""
    depth, quote = 0, None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = None
        elif ch in "\"'" and depth == 0:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth == 0:
            return text[start:i + 1]
    return text[start:]


def clickable_non_controls(text: str) -> list[tuple[int, str]]:
    """``(line, tag)`` of every opening tag in ``text`` that has an ``onClick`` and no ``role``."""
    found = []
    for m in TAGS.finditer(text):
        tag = opening_tag(text, m.start())
        if re.search(r"\bonClick=", tag) and not re.search(r"\brole=", tag):
            found.append((text.count("\n", 0, m.start()) + 1, m.group(1)))
    return found


def test_the_graph_builder_has_no_more_clickable_non_controls_than_its_baseline() -> None:
    counts = {path.name: len(clickable_non_controls(path.read_text(encoding="utf-8"))) for path in sorted(BUILDER.glob("*.jsx"))}
    counts = {name: n for name, n in counts.items() if n}
    assert counts == BASELINE, (
        "a clickable span, div or link with no role was added (use a <button>), or one was fixed (lower the number in BASELINE): "
        f"{ {k: (counts.get(k, 0), BASELINE.get(k, 0)) for k in sorted(set(counts) | set(BASELINE)) if counts.get(k, 0) != BASELINE.get(k, 0)} }"
    )


def test_the_branch_builder_has_none_left() -> None:
    assert clickable_non_controls((BUILDER / "gb-branches.jsx").read_text(encoding="utf-8")) == []


def test_the_scan_sees_what_it_claims_to() -> None:
    text = (
        '<span onClick={() => run()} style={{ a: 1 }}>x</span>\n'
        '<div role="button" tabIndex={0} onClick={go}>y</div>\n'
        '<button onClick={go}>z</button>\n'
        '<a onClick={() => { if (a > b) c(); }} href="#">w</a>\n'
        '<div className="row">no click</div>\n'
    )
    assert clickable_non_controls(text) == [(1, "span"), (4, "a")]
