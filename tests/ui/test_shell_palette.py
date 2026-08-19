"""Palette contract (spec section 8) and its second entry point.

One registry, two entry points: Cmd+K and the composer's "/". A second
row list would be a second ranking, which is how palettes rot.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-palette.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_ranking_comes_from_the_shared_ranker() -> None:
    src = _src()
    assert "SH_rankVerbs" in src
    assert re.search(r"docKind:", src), "context gating needs the focused doc kind"
    assert "frecency" in src


def test_rows_show_their_chord_and_graduation_hint() -> None:
    src = _src()
    assert "verb.chord" in src
    assert "graduation" in src.lower()


def test_a_persistent_chip_advertises_the_palette() -> None:
    src = _src()
    assert 'data-testid="shell-palette-chip"' in src
    assert "Cmd+K" in src or "Ctrl+K" in src


def test_every_chord_points_at_a_registered_verb_id() -> None:
    """The dual-render guard re-checks this against the live registry in P5."""
    src = _src()
    m = re.search(r"var SH_CHORDS = \{([\s\S]*?)\n\};", src)
    assert m, "SH_CHORDS must be a literal map"
    ids = re.findall(r'"[^"]+":\s*"([\w.]+)"', m.group(1))
    assert ids, "no chords bound"
    assert all("." in i for i in ids), f"chord targets must be verb ids: {ids}"


def test_the_composer_slash_reuses_the_same_rows() -> None:
    src = _src()
    assert "window.SH_PaletteRows = SH_PaletteRows;" in src


def test_palette_state_never_reaches_the_url() -> None:
    src = _src()
    assert "pushState" not in src and "location.hash" not in src
