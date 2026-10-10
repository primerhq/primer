"""Every focus-holding layer is a focus trap (review of #729, round 2, the blocker).

The document listener of ``ui/foundation/focus-trap.js`` knows the open TRAPS and nothing else: a layer that holds focus above a trap without being one is ignored, and a Tab from it (or from ``<body>`` while it is
open) goes to the first control of the trap BEHIND its scrim, where the next Enter can close the overlay and lose a draft. Two layers were such: the graph builder's add-step palette (whose second stage disables the
focused search box, so focus falls to ``<body>``) and the Ctrl+K command palette (``role="dialog"``). Both are traps now. These pins keep a ``role="dialog"`` from appearing without one and keep both palettes on the hook;
``tests/ui_e2e/test_focus_trap_layers_journey.py`` presses the keys in the real console.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"

# files with a ``role="dialog"`` element and no ``useFocusTrap`` call, with how many (can only shrink): the trace drawer is a non-modal side panel, not a layer that holds focus above a trap
BASELINE = {"components/console/nv-session-doc.jsx": 1}

DIALOG = re.compile(r'role="(?:dialog|alertdialog)"')


def strip_comments(text: str) -> str:
    """The source with block comments and whole-line ``//`` comments blanked (newlines kept): a comment that mentions ``role="dialog"`` or ``useFocusTrap(`` is not code."""
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _dialogs_without_a_trap() -> dict[str, int]:
    found = {}
    for path in sorted((UI / "components").rglob("*.jsx")):
        text = strip_comments(path.read_text(encoding="utf-8"))
        n = len(DIALOG.findall(text))
        if n and "useFocusTrap(" not in text:
            found[path.relative_to(UI).as_posix()] = n
    return found


def test_no_dialog_is_drawn_without_a_focus_trap() -> None:
    assert _dialogs_without_a_trap() == BASELINE, "a role=dialog element in a file that never calls useFocusTrap (make it a trap), or one got its trap (lower BASELINE)"


def test_the_command_palette_is_a_focus_trap() -> None:
    text = strip_comments((UI / "components" / "console" / "nv-palette.jsx").read_text(encoding="utf-8"))
    assert re.search(r"useFocusTrap\(\s*\w+\s*,\s*open\b", text), "the command palette must trap focus while it is open"
    assert re.search(r'role="dialog"[^>]*aria-label="Command palette"|aria-label="Command palette"[^>]*role="dialog"', text) or 'role="dialog" aria-label="Command palette"' in text


def test_the_add_step_palette_is_a_focus_trap_and_hands_its_second_stage_the_focus() -> None:
    text = strip_comments((UI / "components" / "graph-builder" / "gb-palette.jsx").read_text(encoding="utf-8"))
    assert re.search(r"useFocusTrap\(\s*\w+\s*,\s*true\b", text), "the add-step palette must trap focus while it is open"
    # the second stage disables the focused search box, which drops focus to <body>: the stage hands it to its own first control
    assert re.search(r'stage (?:===|!==) "reference"[\s\S]{0,400}\.focus\(\)', text), "the second stage must move focus into itself"
