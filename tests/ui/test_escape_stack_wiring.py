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


def test_no_component_listens_for_escape_on_the_window_or_the_document() -> None:
    """Every ``keydown`` listener added to the window or the document, anywhere in the UI, must not answer Escape: that is the stack's job. The handler is read back from the listener's name."""
    offenders = []
    for path in sorted(list(UI.rglob("*.jsx")) + list(UI.rglob("*.js"))):
        rel = path.relative_to(UI).as_posix()
        if rel.startswith("vendor/") or rel in {"design-canvas.jsx", "foundation/escape-stack.js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"""(?:window|document)\.addEventListener\(\s*["']keydown["']\s*,\s*(\w+)""", text):
            name = m.group(1)
            defined = [d for d in re.finditer(rf"(?:function {name}\b|(?:const|let|var) {name} =)", text[: m.start()])]
            if not defined:
                continue
            body = text[defined[-1].start(): m.start()]
            if "Escape" in body:
                offenders.append(f"{rel}: {name}")
    assert not offenders, f"these still answer Escape themselves, use useEscape: {offenders}"


def test_an_input_that_handles_escape_itself_says_so_so_the_layer_behind_it_stays() -> None:
    """The add-step palette's search box, the value picker of the reference editor and the session title input each close something of their own on Escape; the layer under them must not hear the same key."""
    palette = _src("components/graph-builder/gb-palette.jsx")
    assert re.search(r'if \(e\.key === "Escape"\) \{ e\.preventDefault\(\); onClose\(\); return; \}', palette)
    refs = _src("components/graph-builder/gb-ref-editor.jsx")
    assert re.search(r'onKeyDown=\{\(e\) => \{ if \(e\.key === "Escape"\) \{ e\.preventDefault\(\); onClose\(\); \} \}\}', refs)
    doc = _src("components/console/nv-session-doc.jsx")
    assert re.search(r'if \(ev\.key === "Escape"\) \{ ev\.preventDefault\(\); setDraft\(null\); \}', doc)


def test_the_comment_that_described_the_two_listeners_does_not_any_more() -> None:
    """admin_users.jsx guarded a Modal over a Modal against the double close; the stack is what prevents it now, and the comment must say what is true."""
    users = _src("components/admin_users.jsx")
    assert "both Modals listen for the same global keydown" not in users
