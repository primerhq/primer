"""A ratchet on the graph builder's clickable things that are not controls (board task 01a12124).

A ``<span onClick>`` or ``<div onClick>`` with no ``role`` is invisible to a keyboard and a screen reader: no focus, no key, no name. The builder had 22 of them; the three the #692 review found
(+ Add a path, Remove this connection, Delete this step) and the Advanced toggle in front of one of them are buttons now, and this file counts what is left, per file. The counts may only go DOWN:
a new one fails, and a file that got below its count fails until the baseline is lowered (so the number is never a cushion for a later one). The rest are listed in ``BASELINE`` for the follow-up that
turns each into a button, a ``role="button"`` with a key handler, or a link.

What is counted (rounds 2 and 3 of #705 hardened it): an opening ``<span>``, ``<div>``, ``<a>``, ``<li>``, ``<p>``, ``<td>``, ``<tr>``, ``<section>``, ``<label>``, ``<ul>``, ``<ol>``, a heading, ``<img>`` or
``<svg>`` whose attributes hold ``onClick``, ``onDoubleClick``, ``onAuxClick``, ``onContextMenu``, ``onMouseDown``, ``onMouseUp``, ``onPointerDown``, ``onPointerUp``, ``onTouchStart`` or ``onTouchEnd`` (or
that handler's ``Capture`` form), and which is not a control. A control is a ``<button>`` (never scanned), a link with an ``href`` that is something (``href={undefined}`` is not), or an element with an
INTERACTIVE ``role`` (button, link, tab, menuitem, checkbox, radio, switch, option ...) AND a tab stop (a ``tabIndex`` of 0 or more: ``-1`` is not one) AND an ``onKeyDown``: a ``role="presentation"`` or
``role="dialog"`` does not make a click handler reachable, and ``data-role=`` is not a role. Comments are not code, and a ``/*`` inside a string does not start one.
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
    r"(?<![\w-])on(?:Click|DoubleClick|AuxClick|ContextMenu|MouseDown|MouseUp|PointerDown|PointerUp|TouchStart|TouchEnd)(?:Capture)?="
)
# a tab stop is a tabIndex of 0 or more that the scan can read: -1 takes focus from a script and a click, not from a Tab
TAB_STOP = re.compile(r'(?<![\w-])tabIndex=(?:\{\s*\d+\s*\}|"\d+")')
# an href with a destination: ``href={undefined}`` and ``href={null}`` render an anchor with none
HREF = re.compile(r"(?<![\w-])href=(?!\{\s*(?:undefined|null)\s*\})")
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
    """``text`` with the comments blanked (newlines kept, so line numbers stay): a block comment anywhere that is not inside a string, and a line comment that starts its line (a ``//`` after code may be
    inside a URL). A quote opens a string that ends at its closing quote or, for ``'`` and ``"``, at the end of the line (an apostrophe in JSX text is not a string); a backtick string may span lines."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
        elif ch in "\"'`":
            j = i + 1
            while j < n and text[j] != ch and (ch == "`" or text[j] != "\n"):
                j += 2 if text[j] == "\\" else 1
            end = j + 1 if j < n and text[j] == ch else j
            out.append(text[i:end])
            i = end
        else:
            out.append(ch)
            i += 1
    return re.sub(r"(?m)^(\s*)//.*$", lambda m: m.group(1), "".join(out))


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
    """``(line, tag)`` of every opening tag in ``text`` that takes a pointer handler and is not a control: not a link with an ``href``, and not an interactive ``role`` with a tab stop (``tabIndex`` of 0 or more) and an ``onKeyDown``."""
    text = strip_comments(text)
    found = []
    for m in TAGS.finditer(text):
        name = m.group(1)
        tag = opening_tag(text, m.start())
        if not HANDLERS.search(tag):
            continue
        if name == "a" and HREF.search(tag):
            continue
        role = re.search(r"(?<![\w-])role=\"(\w+)\"", tag)
        if (
            role
            and role.group(1) in INTERACTIVE
            and TAB_STOP.search(tag)
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


def test_a_tab_index_of_minus_one_is_not_a_tab_stop() -> None:
    """N4' (round 2 of the review): ``tabIndex={-1}`` takes focus from a script and from a click, not from a Tab, so it does not make a role reachable from a keyboard."""
    base = '<div role="button" tabIndex={%s} onKeyDown={key} onClick={go}>x</div>'
    assert clickable_non_controls(base % "-1") == [(1, "div")]
    assert clickable_non_controls(base % "0") == []
    assert (
        clickable_non_controls(
            '<div role="button" tabIndex="0" onKeyDown={key} onClick={go}>x</div>'
        )
        == []
    )
    assert clickable_non_controls(base % "n") == [(1, "div")], (
        "an expression the scan cannot read is not a proof"
    )


def test_the_capture_touch_context_menu_and_aux_handlers_count_too() -> None:
    for handler in (
        "onClickCapture",
        "onMouseDownCapture",
        "onPointerDownCapture",
        "onAuxClick",
        "onContextMenu",
        "onTouchStart",
        "onTouchEnd",
    ):
        assert clickable_non_controls(f"<span {handler}={{go}}>x</span>") == [
            (1, "span")
        ], handler


def test_a_comment_opener_inside_a_string_does_not_hide_the_code_after_it() -> None:
    """N4': ``"/*"`` in a string started a comment that ran to the next ``*/`` anywhere, blanking the code between."""
    assert clickable_non_controls(
        'const a = "/*";\n<span onClick={go}>x</span>\nconst b = "*/";\n'
    ) == [(2, "span")]
    assert clickable_non_controls(
        "const a = '/*';\n<div onClick={go}>x</div>\nconst b = `*/`;\n"
    ) == [(2, "div")]
    assert clickable_non_controls(
        "/* <span onClick={go}>old</span> */\n<span onClick={go}>live</span>\n"
    ) == [(2, "span")], "a real block comment still hides what is in it"
    assert clickable_non_controls(
        "<p>Don't</p>\n<span onClick={go}>x</span>\n<p>it's</p>\n"
    ) == [(2, "span")], "an apostrophe in JSX text is not a string"


def test_an_href_that_is_nothing_does_not_make_a_link() -> None:
    """N4': ``<a href={undefined}>`` renders an anchor with no destination: not focusable, not a link."""
    assert clickable_non_controls("<a href={undefined} onClick={go}>x</a>") == [
        (1, "a")
    ]
    assert clickable_non_controls("<a href={null} onClick={go}>x</a>") == [(1, "a")]
    assert clickable_non_controls("<a href={url} onClick={go}>x</a>") == []
    assert clickable_non_controls("<a href='#/x' onClick={go}>x</a>") == []


def test_an_attribute_that_only_ends_in_a_handler_name_is_not_a_handler() -> None:
    for attr in ("data-onClick", "aria-onClick", "xonClick", "data-onMouseDown"):
        assert clickable_non_controls(f"<div {attr}={{go}}>x</div>") == [], attr
