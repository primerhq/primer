"""Static checks for FC1 — global reduced-motion + a few tasteful transitions.

The stylesheet must ship ONE global (top-level, not nested in a viewport
media query) ``prefers-reduced-motion: reduce`` block that neutralises all
animations/transitions via the universal selector (this also tames the
infinite status-dot ``pulse``). It must also carry a small, restrained set of
transitions: the toast enter (translate-y + opacity), the command-palette
open (opacity + scale), and row-hover background on sidebar/list rows.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "ui" / "styles.css"


def _src() -> str:
    return CSS.read_text(encoding="utf-8")


def test_global_reduced_motion_block_uses_universal_selector() -> None:
    src = _src()
    # A global block targeting *, *::before, *::after with the four
    # motion-neutralising declarations.
    m = re.search(
        r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{\s*"
        r"\*\s*,\s*\*::before\s*,\s*\*::after\s*\{([^}]*)\}",
        src,
    )
    assert m, (
        "styles.css must define a global prefers-reduced-motion block whose "
        "universal selector (*, *::before, *::after) neutralises motion"
    )
    body = m.group(1)
    assert "animation-duration" in body and "!important" in body
    assert "animation-iteration-count" in body
    assert "transition-duration" in body
    assert "scroll-behavior" in body


def test_toast_enter_uses_translate_y_and_opacity() -> None:
    src = _src()
    assert "@keyframes toastIn" in src, "toast enter keyframes must exist"
    m = re.search(r"@keyframes toastIn\s*\{([^}]*\}[^}]*)\}", src)
    assert m and "translateY" in m.group(0) and "opacity" in m.group(0), (
        "toast enter must animate translate-y + opacity (not translateX)"
    )


def test_command_palette_open_scales_in() -> None:
    src = _src()
    assert ".cmd-palette" in src, "the command palette needs its own animation hook"
    assert "@keyframes paletteIn" in src
    m = re.search(r"@keyframes paletteIn\s*\{([^}]*\}[^}]*)\}", src)
    assert m and "scale(0.98)" in m.group(0) and "opacity" in m.group(0), (
        "command-palette open must fade + scale from .98"
    )


def test_sidebar_and_list_rows_have_hover_transition() -> None:
    src = _src()
    # nav-item carries a background+color transition (~150ms).
    assert re.search(r"\.nav-item\s*\{[^}]*transition:\s*background\s*0\.15s", src), (
        ".nav-item must ease its hover background over ~150ms"
    )
    # studio workspace-menu rows fade their hover background in too.
    assert re.search(r"\.st-ws-menu-row\s*\{[^}]*transition:\s*background\s*0\.15s", src), (
        ".st-ws-menu-row must ease its hover background over ~150ms"
    )
