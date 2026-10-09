"""The close buttons of the console's dialogs and toasts name themselves (console review C-003, found by the runtime sweep of the main surfaces).

``Modal`` (``shared.jsx``) and the semantic-search dialog drew their close control as ``<button className="close"><Icon name="x" /></button>``: an icon and no text, so a screen reader met an
unnamed button on EVERY dialog of the console (ten surfaces in the sweep). The toast's close (``nv-shell.jsx``) was the letter ``x``, which is a name that says nothing. Each now says what it does.

Static pins on each site, in the same slicing style as the rest of ``tests/ui``; ``tests/ui_e2e/test_modal_close_button_has_a_name_journey.py`` checks a real dialog in a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "components"


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
