"""The close buttons of the console's dialogs and toasts name themselves (console review C-003, found by the runtime sweep of the main surfaces).

``Modal`` (``shared.jsx``) and the semantic-search dialog drew their close control as ``<button className="close"><Icon name="x" /></button>``: an icon and no text, so a screen reader met an
unnamed button on EVERY dialog of the console (ten surfaces in the sweep). The toast's close (``nv-shell.jsx``) was the letter ``x``, which is a name that says nothing. Each now says what it does.

The console's glyph-only ``x`` close buttons (audit G9-4) are pinned the same way: every ``x`` glyph close carries a title or aria-label on its own tag, and the graph view overlay's close
carries a data-testid so the sweep and e2e can target it.

Static pins on each site, in the same slicing style as the rest of ``tests/ui``; ``tests/ui_e2e/test_modal_close_button_has_a_name_journey.py`` checks a real dialog in a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"

# the glyph as written in the console's close buttons (the multiplication sign)
XGLYPH = "\u00d7"
X_CLOSE = re.compile(re.escape(XGLYPH) + r"</button>")


def _close_buttons(path: Path) -> list[str]:
    """Every ``<button ... className="close" ...>...</button>`` of the file, as written."""
    src = path.read_text(encoding="utf-8")
    return re.findall(r'<button[^>]*className="close"[^>]*>.*?</button>', src, re.S)


def test_the_modal_close_button_is_named() -> None:
    found = _close_buttons(UI / "shared.jsx")
    assert len(found) == 1, found
    assert re.search(r'aria-label="Close"', found[0])


def test_the_semantic_search_dialog_close_button_is_named() -> None:
    found = _close_buttons(UI / "semantic-search.jsx")
    assert len(found) == 1, found
    assert re.search(r'aria-label="Close"', found[0])


def test_the_toast_close_button_says_what_it_does() -> None:
    found = _close_buttons(UI / "console" / "nv-shell.jsx")
    assert len(found) == 1, found
    assert re.search(r'aria-label="Dismiss"', found[0]), "the toast's close is the letter x"


def test_no_other_close_button_without_text_or_a_name_is_left() -> None:
    """The scan itself: a ``className="close"`` button needs an ``aria-label`` or visible words, wherever it is drawn."""
    bare = []
    for path in sorted(UI.rglob("*.jsx")):
        for button in _close_buttons(path):
            inner = re.sub(r"<[^>]+>", "", button.split(">", 1)[1].rsplit("</button>", 1)[0]).strip()
            if "aria-label=" not in button and not inner:
                bare.append((path.name, button[:80]))
    assert not bare, bare


# G9-4 (audit): the console's glyph-only "x" close buttons that announce as "x" to a screen
# reader (no title, no aria-label on the button's own tag). Every other console close button
# is named; these four gaps are pinned here, and the graph view's close also needs a
# data-testid so the sweep and e2e can target it.
CONSOLE = UI / "console"


def _x_glyph_close_windows(src: str) -> list[str]:
    """Every glyph ``x</button>`` close of the file: the window from the last ``<button`` before the glyph to the glyph, the opening tag as written."""
    windows = []
    for m in X_CLOSE.finditer(src):
        back = src[max(0, m.start() - 400):m.start()]
        start = back.rfind("<button")
        assert start != -1, back[-120:]
        windows.append(back[start:])
    return windows


def test_every_x_glyph_close_button_in_the_console_is_named() -> None:
    bare = []
    for path in sorted(CONSOLE.glob("*.jsx")):
        for window in _x_glyph_close_windows(path.read_text(encoding="utf-8")):
            if "title=" not in window and "aria-label=" not in window:
                bare.append(path.name)
    assert not bare, bare


def test_the_graph_view_close_button_has_a_testid() -> None:
    src = (CONSOLE / "nv-session-doc.jsx").read_text(encoding="utf-8")
    windows = _x_glyph_close_windows(src[src.index("nv-graph-overlay"):])
    assert windows, "no glyph close inside the graph view overlay"
    assert "data-testid=" in windows[0], windows[0][:120]
