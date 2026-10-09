"""Every native control of the live graph builder has a name (console review C-003, the graph builder).

The graph editor is ``GB_Builder`` (``ui/components/graph-builder/gb-*.jsx``). Its compact UI draws
no visible label for most controls: a title input, a branch row of four selects and a value, a fan-out spec, a schema row, the picker and palette search boxes. A placeholder is the only text of
an input, and a ``<select>`` has none: a screen reader met "combo box" five times in one branch editor. Each control now carries an ``aria-label`` (or sits inside a wrapping ``<label>``).

This is the static half: every ``<input>``, ``<select>`` and ``<textarea>`` of the builder's files, and of the shared ``EntityPicker`` its pickers use, is named or wrapped. The reference
editor's contentEditable body is the builder's one control that is not a native element, so the scan cannot see it: the #668 review (2026-10-09) found its accessible name empty. It is a
``role="textbox"`` with ``aria-multiline`` named by the ``label`` its row shows (the section title, or the tool-argument name the row draws); it runs in V8 on the hook runtime of
``tests/ui/_mini_react.py``, and its call sites are pinned to pass that label. The browser half
(``tests/ui_e2e/test_graph_builder_named_controls_journey.py``) sweeps every node kind of a seeded graph for what is really on the page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

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


# ---------------------------------------------------------------------------
# GB_RefEditor: the contentEditable body the native-control scan cannot see
# ---------------------------------------------------------------------------

REF_EDITOR = UI / "graph-builder" / "gb-ref-editor.jsx"

# The globals GB_RefEditor declares live in gb-refs.jsx; a fresh editor touches none of them
# before the user types, so plain stubs stand in for that file.
_REF_PRELUDE = r"""
var GB_parseTemplate = function (s) { return []; };
var GB_serialize = function (tokens) { return ""; };
var GB_availableRefs = function (draft, nodeId) { return []; };
var GB_chipLabel = function (draft, token) { return String(token.v); };
var GB_refIsBroken = function (draft, token) { return false; };
"""


def test_the_ref_editor_is_a_named_multiline_textbox() -> None:
    """The contentEditable body is the graph builder's one non-native control: a multiline textbox named by the label its row shows."""
    ctx = mini_react_context(transpile(REF_EDITOR), _REF_PRELUDE)
    try:
        ctx.eval(
            "MR.mount(GB_RefEditor, { value: '', onChange: function () {}, draft: {}, nodeId: 'n1', label: 'What it gets' });"
            "var node = MR.find('gb-ref-editor');"
            "window.__got = JSON.stringify({ role: node.props.role, multiline: node.props['aria-multiline'], name: node.props['aria-label'] });"
        )
        got = json.loads(ctx.eval("window.__got"))
        assert got.get("role") == "textbox"
        assert got.get("multiline") is True
        assert got.get("name") == "What it gets"
    finally:
        ctx.close()


def test_every_ref_editor_call_site_passes_the_label_its_row_shows() -> None:
    """Each of the five GB_RefEditor call sites passes the label the surrounding row shows, so the editor's name is never a bare fallback."""
    src = (UI / "graph-builder" / "gb-inspector.jsx").read_text(encoding="utf-8")
    sites = []
    for m in re.finditer(r"<GB_RefEditor\b", src):
        tag = src[m.start():_tag_end(src, m.start())]
        sites.append(" ".join(tag.split())[:120])
        assert "label=" in tag, f"a GB_RefEditor call without the label its row shows:\n{sites[-1]}"
    assert len(sites) == 5, "the call-site count drifted; a new GB_RefEditor needs the label its row shows"
