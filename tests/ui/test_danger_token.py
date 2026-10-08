"""The console declares --danger in both theme blocks, so var(--danger, ...) stops falling back to #e06c5f.

``--danger`` was referenced throughout ``ui/styles.css`` (the turn error, the tool-block error, the error chips, the cancelled lifecycle dot) but never declared, so every use rendered the fallback ``#e06c5f`` in both themes. Each theme block now declares the token from its own ``--red``, so the dark theme renders the dark ``--red`` and the light theme renders the light ``--red``. This test pins the declaration in both blocks and the fallback discipline of the uses; plain string work, no browser (ticket of the 2026-10-08 admin review).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")


def _theme_block(css: str, selector: str) -> str:
    """The rule whose selector line contains ``selector``, from the selector to and including its closing brace."""
    start = css.index(selector)
    open_brace = css.index("{", start)
    depth = 0
    for i in range(open_brace, len(css)):
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        if depth == 0:
            return css[start:i + 1]
    raise AssertionError(f"no closing brace for {selector!r}")


def test_both_theme_blocks_declare_the_danger_token() -> None:
    dark = _theme_block(CSS, ':root[data-theme="dark"] {')
    light = _theme_block(CSS, ':root[data-theme="light"] {')

    assert "--danger:" in dark, "the dark block declares --danger from its own --red"
    assert "--danger:" in light, "the light block declares --danger from its own --red"


def test_every_danger_use_keeps_its_fallback() -> None:
    uses = re.findall(r"var\(--danger[^)]*\)", CSS)

    assert uses, "the var(--danger uses are gone"
    for use in uses:
        assert use.startswith("var(--danger,"), f"the use lost its fallback: {use}"
