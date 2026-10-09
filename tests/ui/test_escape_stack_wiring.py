"""Everything that closes on Escape goes through the one Escape stack (board task 01a12147).

``tests/ui/test_escape_stack_behaviour.py`` runs the stack. These pins are for the wiring that a behaviour test of the stack cannot tell apart from a hand-rolled listener: the file is loaded before
the components that call it, each layer that closes on Escape registers with ``useEscape`` and no component adds a window or document ``keydown`` listener that answers Escape (that was the
defect: two listeners, one key, two layers closed), and an input that handles Escape itself says so with ``preventDefault`` so that the stack does not also close the layer behind it. What it
DOES for the user is driven in a real browser by ``tests/ui_e2e/test_escape_closes_one_layer_journey.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
INDEX = (UI / "index.html").read_text(encoding="utf-8")


def _src(path: str) -> str:
    return (UI / path).read_text(encoding="utf-8")


def _function(src: str, header: str) -> str:
    """The text from ``header`` to the closing brace of its body (the first ``{`` after the parameter list, which may itself be a destructuring)."""
    start = src.index(header)
    depth, i = 0, start + re.compile(r"\)\s*(?:=>\s*)?\{").search(src[start:]).end() - 1
    while True:
        depth += src[i] == "{"
        depth -= src[i] == "}"
        i += 1
        if depth == 0:
            return src[start:i]


def test_the_stack_is_loaded_before_every_component_that_uses_it() -> None:
    assert 'src="foundation/escape-stack.js"' in INDEX
    at = INDEX.index('src="foundation/escape-stack.js"')
    for user in ("components/shared.jsx", "components/shared/bottom-sheet.jsx", "components/console/nv-overlays.jsx", "components/console/nv-palette.jsx",
                 "components/console/nv-chrome.jsx", "components/console/nv-session-doc.jsx"):
        assert at < INDEX.index(f'src="{user}"'), user


def test_the_modal_the_overlay_panel_the_sheet_the_palette_the_lightbox_and_the_menus_register_with_the_stack() -> None:
    shared = _src("components/shared.jsx")
    assert "useEscape(" in shared[shared.index("const Modal = ("):shared.index("const _dialogState")]
    assert "useEscape(" in _function(_src("components/console/nv-overlays.jsx"), "function NV_OverlayPanel(")
    assert "useEscape(" in _function(_src("components/shared/bottom-sheet.jsx"), "function BottomSheet(")
    assert "useEscape(" in _function(_src("components/console/nv-session-doc.jsx"), "function NV_Lightbox(")
    assert "useEscape(" in _function(_src("components/console/nv-chrome.jsx"), "function NV_useMenuDismiss(")
    assert "useEscape(" in _src("components/console/nv-palette.jsx")


def test_a_layer_is_on_the_stack_only_while_it_is_open() -> None:
    sheet = _function(_src("components/shared/bottom-sheet.jsx"), "function BottomSheet(")
    assert re.search(r"useEscape\([\s\S]*?!!open\)", sheet), "the sheet stays mounted while closed"
    menu = _function(_src("components/console/nv-chrome.jsx"), "function NV_useMenuDismiss(")
    assert re.search(r"useEscape\([\s\S]*,\s*open\)", menu), "a menu answers Escape only while it is open"
    assert re.search(r"useEscape\([\s\S]*,\s*open\)", _src("components/console/nv-palette.jsx")), "the palette answers Escape only while it is open"


_LISTENER = re.compile(r"""(?:window|document)\.addEventListener\(\s*["'](?:keydown|keyup|keypress)["']\s*,\s*""")
_ESCAPE = re.compile(r"""["']Escape["']|["']Esc["']|keyCode\s*[=!]==?\s*27|\bwhich\s*[=!]==?\s*27""")


def _matching_paren(text: str, open_at: int) -> int:
    depth, i = 0, open_at
    while i < len(text):
        depth += text[i] == "("
        depth -= text[i] == ")"
        if depth == 0:
            return i
        i += 1
    return len(text)


def listeners_that_answer_escape(text: str) -> list[str]:
    """The window or document ``keydown``/``keyup``/``keypress`` listeners in ``text`` whose handler (a named function defined above the call, or the inline function in the call) mentions the Escape key
    in any of its spellings (``"Escape"``, ``"Esc"``, ``keyCode === 27``, ``which === 27``)."""
    found = []
    for m in _LISTENER.finditer(text):
        call_open = text.rindex("(", m.start(), m.end())
        call = text[m.end():_matching_paren(text, call_open)]
        name = re.match(r"(\w+)\s*$", call.split(",")[0].strip())
        if name and re.fullmatch(r"\w+", call.strip().split(",")[0].strip()):
            defined = list(re.finditer(rf"(?:function {name.group(1)}\b|(?:const|let|var) {name.group(1)} =)", text[: m.start()]))
            body = text[defined[-1].start(): m.start()] if defined else ""
        else:
            body = call                                           # an inline function
        if _ESCAPE.search(body):
            found.append(text[m.start(): m.start() + 60].replace("\n", " "))
    return found


def test_no_component_listens_for_escape_on_the_window_or_the_document() -> None:
    """Every ``keydown``, ``keyup`` or ``keypress`` listener added to the window or the document, anywhere in the UI, must not answer Escape (named or inline handler, ``"Escape"``, ``"Esc"`` or key code
    27): that is the stack's job. What the scan does NOT see: a listener added to an element, or to something other than the window and the document, and a key compared by a variable."""
    offenders = []
    for path in sorted(list(UI.rglob("*.jsx")) + list(UI.rglob("*.js"))):
        rel = path.relative_to(UI).as_posix()
        if rel.startswith("vendor/") or rel in {"design-canvas.jsx", "foundation/escape-stack.js"}:
            continue
        offenders += [f"{rel}: {hit}" for hit in listeners_that_answer_escape(path.read_text(encoding="utf-8"))]
    assert not offenders, f"these still answer Escape themselves, use useEscape: {offenders}"


def test_the_scan_sees_the_shapes_it_claims_to_and_not_the_ones_it_does_not() -> None:
    named = "function onKey(ev) { if (ev.key === 'Escape') close(); }\nwindow.addEventListener('keydown', onKey);"
    inline = "document.addEventListener(\"keyup\", (e) => { if (e.key === \"Escape\") close(); });"
    keycode = "window.addEventListener('keydown', function (e) { if (e.keyCode === 27) close(); });"
    esc = "window.addEventListener('keypress', (e) => { if (e.key === 'Esc') close(); });"
    other = "function onKey(ev) { if (ev.ctrlKey && ev.key === 'k') open(); }\nwindow.addEventListener('keydown', onKey);"
    inline_other = "window.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });"
    for text in (named, inline, keycode, esc):
        assert len(listeners_that_answer_escape(text)) == 1, text
    for text in (other, inline_other):
        assert listeners_that_answer_escape(text) == [], text


def test_an_input_that_handles_escape_itself_says_so_so_the_layer_behind_it_stays() -> None:
    """The reference editor's value picker field and the session title input each close something of their own on Escape; the layer under them must not hear the same key."""
    refs = _src("components/graph-builder/gb-ref-editor.jsx")
    assert re.search(r'if \(e\.key === "Escape" && picker\) \{ e\.preventDefault\(\); setPicker\(null\); \}', refs)
    doc = _src("components/console/nv-session-doc.jsx")
    assert re.search(r'if \(ev\.key === "Escape"\) \{ ev\.preventDefault\(\); setDraft\(null\); \}', doc)


def test_the_add_step_palette_is_a_layer_of_the_stack_not_a_key_handler_of_its_search_box() -> None:
    """Review of #700, B2: in its second stage nothing in the palette has focus (the search box is disabled), so an element-level Escape never ran and the key went to the graph overlay under it."""
    palette = _src("components/graph-builder/gb-palette.jsx")
    body = _function(palette, "function GB_AddStepPalette(")
    assert re.search(r"window\.primerApi\.useEscape\(\s*onClose\s*\)", body)
    assert 'e.key === "Escape"' not in body, "one mechanism: the stack"


def test_the_other_popups_are_layers_of_the_stack_too() -> None:
    """Review of #700, N5: a popup answers Escape from wherever focus is, and only while it is open."""
    overlays = _src("components/console/nv-overlays.jsx")
    create = overlays[overlays.index("function NV_CreateSessionOverlay"):overlays.index("function NV_CreateWorkspaceOverlay")]
    assert re.search(r"window\.primerApi\.useEscape\([\s\S]*?setMenuOpen\(false\)[\s\S]*?,\s*menuOpen\)", create), "the binding menu of the Create session overlay, while it is open"
    assert "useEscape(" in _function(_src("components/graph-builder/gb-readiness.jsx"), "function GB_ReadinessPopover(")
    assert "useEscape(" in _function(_src("components/graph-builder/gb-ref-editor.jsx"), "function GB_RefPicker(")


def test_the_comment_that_described_the_two_listeners_does_not_any_more() -> None:
    """admin_users.jsx guarded a Modal over a Modal against the double close; the stack is what prevents it now, and the comment must say what is true."""
    users = _src("components/admin_users.jsx")
    assert "both Modals listen for the same global keydown" not in users
