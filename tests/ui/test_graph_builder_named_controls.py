"""Every native control of the live graph builder has a name (console review C-003, the graph builder).

The graph editor is ``GB_Builder`` (``ui/components/graph-builder/gb-*.jsx``). Its compact UI draws
no visible label for most controls: a title input, a branch row of four selects and a value, a fan-out spec, a schema row, the picker and palette search boxes. A placeholder is the only text of
an input, and a ``<select>`` has none: a screen reader met "combo box" five times in one branch editor. Each control now carries an ``aria-label`` (or sits inside a wrapping ``<label>``).

This is the static half: every ``<input>``, ``<select>`` and ``<textarea>`` of the builder's files, and of the shared ``EntityPicker`` its pickers use, is named or wrapped. The browser half
(``tests/ui_e2e/test_graph_builder_named_controls_journey.py``) sweeps every node kind of a seeded graph for what is really on the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"
FILES = sorted((UI / "graph-builder").glob("*.jsx")) + [UI / "shared" / "entity-picker.jsx"]


def _tag_end(src: str, start: int) -> int:
    """Index just past the ``>`` that closes the opening tag: a ``>`` inside ``{...}`` or a quoted value does not close it."""
    depth, quote, i = 0, "", start
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth <= 0:
            return i + 1
        i += 1
    return len(src)


def unnamed_controls(src: str) -> list[tuple[int, str]]:
    """(line, tag) of each ``<input|select|textarea>`` with no ``aria-label``, ``aria-labelledby`` or ``id`` that is not inside a ``<label>`` wrapper (a hidden input needs no name)."""
    found = []
    # a tag named in a comment is prose, not a control; the newlines stay so the line numbers still point at the file
    src = re.sub(r"^[ \t]*//.*$", "", src, flags=re.M)
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    for m in re.finditer(r"<(input|select|textarea)\b", src):
        tag = src[m.start():_tag_end(src, m.end())]
        before = src[:m.start()]
        if before.rfind("<label") > before.rfind("</label>"):
            continue
        if re.search(r'\btype="hidden"', tag) or re.search(r"\b(aria-label|aria-labelledby|id)=", tag):
            continue
        found.append((src.count("\n", 0, m.start()) + 1, " ".join(tag.split())[:90]))
    return found


def test_the_scan_itself_sees_a_bare_control_and_lets_a_named_or_wrapped_one_pass() -> None:
    assert unnamed_controls("<input value={a} />\n<select>\n</select>") == [(1, "<input value={a} />"), (2, "<select>")]
    assert unnamed_controls('<input aria-label="x" /><select aria-labelledby="y"></select><textarea id="z" />') == []
    assert unnamed_controls("<label><span>a</span><input onChange={(e) => go(1)} /></label><input />") == [(1, "<input />")]
    assert unnamed_controls('<input type="hidden" />') == []


def test_the_scan_does_not_count_a_tag_named_in_a_comment_and_keeps_its_line_numbers() -> None:
    assert unnamed_controls("// dumped into a <select> pattern\n/* an <input> here */\n{/* a <textarea> */}\n<input />") == [(4, "<input />")]
    assert unnamed_controls("/* one\n two <select> */\n<select>\n</select>") == [(3, "<select>")]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_control_of_the_file_is_named_or_wrapped(path: Path) -> None:
    assert unnamed_controls(path.read_text(encoding="utf-8")) == []


def test_the_scan_covers_the_builder_and_its_shared_picker() -> None:
    names = {p.name for p in FILES}
    assert {"gb-inspector.jsx", "gb-branches.jsx", "gb-schema.jsx", "gb-palette.jsx", "gb-dryrun.jsx", "gb-ref-editor.jsx", "entity-picker.jsx"} <= names


def test_a_branch_row_names_each_of_its_five_controls_by_branch_and_condition() -> None:
    src = (UI / "graph-builder" / "gb-branches.jsx").read_text(encoding="utf-8")
    for needle in (
        'aria-label={"Branch " + (bi + 1) + ", condition " + (ci + 1) + ": field"}',
        'aria-label={"Branch " + (bi + 1) + ", condition " + (ci + 1) + ": operator"}',
        'aria-label={"Branch " + (bi + 1) + ", condition " + (ci + 1) + ": value"}',
        'aria-label={"Branch " + (bi + 1) + ": go to"}',
        'aria-label="In any other case: go to"',
    ):
        assert needle in src, needle


def test_the_entity_picker_names_its_search_by_its_label_or_its_placeholder() -> None:
    src = (UI / "shared" / "entity-picker.jsx").read_text(encoding="utf-8")
    assert re.search(r"<label className=\"field-label\" htmlFor=\{inputId\}>\{label\}</label>", src), "a label it draws points at the search box"
    assert "aria-label={label ? undefined : (props.ariaLabel || placeholder)}" in src
