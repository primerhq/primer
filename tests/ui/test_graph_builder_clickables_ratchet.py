"""A ratchet on the graph builder's clickable things that are not controls (board task 01a12124).

A ``<span onClick>`` or ``<div onClick>`` with no ``role`` is invisible to a keyboard and a screen reader: no focus, no key, no name. The builder had 22 of them; the three the #692 review found
(+ Add a path, Remove this connection, Delete this step) and the Advanced toggle in front of one of them are buttons now, and this file counts what is left, per file. The counts may only go DOWN:
a new one fails, and a file that got below its count fails until the baseline is lowered (so the number is never a cushion for a later one). The rest are listed in ``BASELINE`` for the follow-up that
turns each into a button, a ``role="button"`` with a key handler, or a link.

What is counted (round 2 of #705 hardened it): an opening ``<span>``, ``<div>``, ``<a>``, ``<li>``, ``<p>``, ``<td>``, ``<tr>``, ``<section>``, ``<label>``, ``<ul>``, ``<ol>``, a heading, ``<img>`` or ``<svg>``
whose attributes hold ``onClick``, ``onDoubleClick``, ``onMouseDown``, ``onMouseUp``, ``onPointerDown`` or ``onPointerUp``, and which is not a control. A control is a ``<button>`` (never scanned), a link
with an ``href``, or an element with an INTERACTIVE ``role`` (button, link, tab, menuitem, checkbox, radio, switch, option ...) AND a ``tabIndex`` AND an ``onKeyDown``: a ``role="presentation"`` or
``role="dialog"`` does not make a click handler reachable, and ``data-role=`` is not a role. Comments are not code.
"""

from __future__ import annotations

import re
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[2] / "ui" / "components" / "graph-builder"

# the clickable non-controls that are still there, per file (can only shrink)
BASELINE = {
    "gb-dryrun.jsx": 2,  # the close x, a node row
    "gb-inspector.jsx": 2,  # the template-kind cards, the fan-out spec chips
    "gb-outline.jsx": 1,  # the step rows
    "gb-palette.jsx": 4,  # the scrim, a purpose row, Back, a tool row
    "gb-readiness.jsx": 1,  # the readiness chip
    "gb-ref-editor.jsx": 2,  # the "/" hint, a value row
    "gb-schema.jsx": 4,  # the JSON toggles, required, remove a field
    "gb-starters.jsx": 1,  # a starter card
    "graph-builder.jsx": 1,  # the bands toggle
}

TAGS = re.compile(r"<(span|div|a|li|p|td|tr|section|label|ul|ol|h[1-6]|img|svg)\b")
# the attribute name must stand alone: not data-onClick, not aria-onClick
HANDLERS = re.compile(
    r"(?<![\w-])on(?:Click|DoubleClick|MouseDown|MouseUp|PointerDown|PointerUp)="
)
# a role that says "I am a control"; presentation, dialog, group, region ... do not make a click handler reachable
INTERACTIVE = {
    "button",
    "link",
    "tab",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "checkbox",
    "radio",
    "switch",
    "option",
    "combobox",
    "textbox",
    "slider",
    "searchbox",
    "treeitem",
}


def strip_comments(text: str) -> str:
    """``text`` with the comments blanked (newlines kept, so line numbers stay): a block comment anywhere, and a line comment that starts its line (a ``//`` after code may be inside a URL)."""
    text = re.sub(
        r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S
    )
    return re.sub(r"(?m)^(\s*)//.*$", lambda m: m.group(1), text)


def opening_tag(text: str, start: int) -> str:
    """The opening tag that begins at ``start``: up to the first ``>`` outside braces and quotes (an arrow function in ``onClick={() => ...}`` is inside braces)."""
    depth, quote = 0, None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = None
        elif ch in "\"'`" and depth == 0:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth == 0:
            return text[start : i + 1]
    return text[start:]


def clickable_non_controls(text: str) -> list[tuple[int, str]]:
    """``(line, tag)`` of every opening tag in ``text`` that takes a pointer handler and is not a control: not a link with an ``href``, and not an interactive ``role`` with a ``tabIndex`` and an ``onKeyDown``."""
    text = strip_comments(text)
    found = []
    for m in TAGS.finditer(text):
        name = m.group(1)
        tag = opening_tag(text, m.start())
        if not HANDLERS.search(tag):
            continue
        if name == "a" and re.search(r"(?<![\w-])href=", tag):
            continue
        role = re.search(r"(?<![\w-])role=\"(\w+)\"", tag)
        if (
            role
            and role.group(1) in INTERACTIVE
            and re.search(r"(?<![\w-])tabIndex=", tag)
            and re.search(r"(?<![\w-])onKeyDown=", tag)
        ):
            continue
        found.append((text.count("\n", 0, m.start()) + 1, name))
    return found


def test_the_graph_builder_has_no_more_clickable_non_controls_than_its_baseline() -> (
    None
):
    counts = {
        path.name: len(clickable_non_controls(path.read_text(encoding="utf-8")))
        for path in sorted(BUILDER.glob("*.jsx"))
    }
    counts = {name: n for name, n in counts.items() if n}
    assert counts == BASELINE, (
        "a clickable span, div or link with no role was added (use a <button>), or one was fixed (lower the number in BASELINE): "
        f"{ {k: (counts.get(k, 0), BASELINE.get(k, 0)) for k in sorted(set(counts) | set(BASELINE)) if counts.get(k, 0) != BASELINE.get(k, 0)} }"
    )


def test_the_branch_builder_has_none_left() -> None:
    assert (
        clickable_non_controls(
            (BUILDER / "gb-branches.jsx").read_text(encoding="utf-8")
        )
        == []
    )


def test_the_scan_sees_what_it_claims_to() -> None:
    text = (
        "<span onClick={() => run()} style={{ a: 1 }}>x</span>\n"
        '<div role="button" tabIndex={0} onKeyDown={key} onClick={go}>y</div>\n'
        "<button onClick={go}>z</button>\n"
        '<a onClick={() => { if (a > b) c(); }} href="#">w</a>\n'
        "<a onClick={go}>no href</a>\n"
        '<div className="row">no click</div>\n'
    )
    assert clickable_non_controls(text) == [(1, "span"), (5, "a")]


def test_a_role_that_is_not_interactive_does_not_make_a_click_reachable() -> None:
    assert clickable_non_controls('<div role="presentation" onClick={go}>x</div>') == [
        (1, "div")
    ]
    assert clickable_non_controls('<div role="dialog" onClick={go}>x</div>') == [
        (1, "div")
    ]
    assert clickable_non_controls('<div data-role="button" onClick={go}>x</div>') == [
        (1, "div")
    ], "data-role is not a role"
    assert clickable_non_controls('<div aria-role="button" onClick={go}>x</div>') == [
        (1, "div")
    ]


def test_an_interactive_role_needs_a_tab_stop_and_a_key_handler() -> None:
    assert clickable_non_controls('<div role="button" onClick={go}>x</div>') == [
        (1, "div")
    ]
    assert clickable_non_controls(
        '<div role="button" tabIndex={0} onClick={go}>x</div>'
    ) == [(1, "div")], "focusable, but Enter does nothing"
    assert clickable_non_controls(
        '<div role="button" onKeyDown={key} onClick={go}>x</div>'
    ) == [(1, "div")], "keys, but nothing to tab to"
    assert (
        clickable_non_controls(
            '<div role="button" tabIndex={0} onKeyDown={key} onClick={go}>x</div>'
        )
        == []
    )


def test_every_pointer_handler_counts_not_only_click() -> None:
    for handler in (
        "onDoubleClick",
        "onMouseDown",
        "onMouseUp",
        "onPointerDown",
        "onPointerUp",
    ):
        assert clickable_non_controls(f"<span {handler}={{go}}>x</span>") == [
            (1, "span")
        ], handler
    assert clickable_non_controls("<span onMouseEnter={go}>x</span>") == [], (
        "hover alone is not a click"
    )


def test_a_link_with_an_href_is_a_control_and_one_without_is_not() -> None:
    assert clickable_non_controls('<a href="#/x" onClick={go}>x</a>') == []
    assert clickable_non_controls("<a onClick={go}>x</a>") == [(1, "a")]


def test_comments_are_not_code() -> None:
    text = (
        "// <span onClick={go}>commented out</span>\n"
        "/* <div onClick={go}>also</div> */\n"
        "{/* <span onMouseDown={go}>in jsx</span> */}\n"
        "<span onClick={go}>real</span>\n"
    )
    assert clickable_non_controls(text) == [(4, "span")]


def test_headings_images_and_lists_that_are_clickable_count_too() -> None:
    for tag in ("h3", "img", "svg", "ul", "ol"):
        assert clickable_non_controls(f"<{tag} onClick={{go}}>x</{tag}>") == [
            (1, tag)
        ], tag
