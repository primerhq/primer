"""The shell's canvas state (pivot P5 Task 18): the grammar's ``canvas=`` is read on navigation, kept beside the anchor, written back by the URL-write effect, exposed on the console context (``con.canvas`` / ``con.setCanvas``, a replace, not a history push), and a doc change NOT made by the URL resets it, so a stale value never rides the URL write onto another document.

The grammar itself (parse/build/drop rules, the manifest mirror) is pinned by ``test_shell_url.py``; these are source slices of the shell wiring, executed nowhere - the shell is a React component, so its seams are read as written.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "ui" / "components" / "console" / "nv-shell.jsx"
SRC = SHELL.read_text(encoding="utf-8")


def test_the_shell_reads_the_canvas_state_from_the_url() -> None:
    # The navigation listener applies it beside the anchor.
    assert "setCanvas(parsed.canvas)" in SRC


def test_the_shell_keeps_the_canvas_state_beside_the_anchor() -> None:
    assert re.search(r"var canvasState = React\.useState\(initial\.canvas\);", SRC)
    assert re.search(r"var canvas = canvasState\[0\];\s*var setCanvas = canvasState\[1\];", SRC)


def test_the_write_effect_passes_the_canvas_state_and_lists_it_in_its_deps() -> None:
    # The grammar writes it only for a session doc; the effect must hand it over and re-run when it moves.
    assert "canvas: canvas," in SRC
    assert re.search(r"anchor, canvas\]\);", SRC)


def test_the_context_exposes_canvas_and_setCanvas_as_a_replace() -> None:
    ctx_block = SRC[SRC.index("var ctx = React.useMemo") : SRC.index("var viewName = ctx.view.name;")]
    assert "canvas: canvas," in ctx_block
    # A replace, not a history push: the bare setter is exposed (no wrapper that would markPush).
    assert re.search(r"setCanvas: setCanvas,", ctx_block)
    line = [l for l in ctx_block.splitlines() if "setCanvas: setCanvas," in l][0]
    assert "markPush" not in line


def test_a_doc_change_not_made_by_the_url_resets_the_canvas_state() -> None:
    # The listener marks the doc change it makes; the reset effect consumes the mark and resets the rest.
    assert "docFromUrlRef.current = true" in SRC
    assert re.search(r"if \(docFromUrlRef\.current\) \{\s*docFromUrlRef\.current = false;\s*return;\s*\}\s*setCanvas\(null\);", SRC)
