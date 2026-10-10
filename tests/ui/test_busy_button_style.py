"""The look of a busy button is pinned (review of #732, N5 and N6).

``Btn busy`` is ``aria-disabled`` and not ``disabled``, so the ``:disabled`` look does not reach it: ``.btn[aria-disabled="true"]`` has to dim it, and nothing but this file noticed when the rule was dropped. The button KEEPS the
focus, and ``opacity`` applies to its outline too, so a busy button's ring was drawn at 45% (barely there on the dialog's footer): the focused busy button is dimmed less and carries an outline of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[2] / "ui" / "styles.css").read_text(encoding="utf-8")


def _rules(selector: str) -> list[str]:
    """The declaration blocks of every rule whose selector list holds ``selector`` exactly."""
    found = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            found.append(m.group(2))
    return found


def test_a_busy_button_looks_disabled() -> None:
    blocks = _rules('.btn[aria-disabled="true"]')
    assert blocks and any("opacity" in b and "cursor: not-allowed" in b for b in blocks), blocks


def test_the_focus_ring_of_a_busy_button_is_visible() -> None:
    blocks = _rules('.btn[aria-disabled="true"]:focus-visible')
    assert blocks, 'no .btn[aria-disabled="true"]:focus-visible rule: the ring of a focused busy button is drawn at the button\'s 45% opacity'
    body = " ".join(blocks)
    assert "outline: 2px solid var(--accent)" in body and "outline-offset" in body, body
    opacity = re.search(r"opacity:\s*([0-9.]+)", body)
    assert opacity and float(opacity.group(1)) >= 0.75, "the focused busy button must be dimmed less than a button nobody can reach"
