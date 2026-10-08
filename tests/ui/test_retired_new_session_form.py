"""``SharedNewSessionForm`` is gone; ``new-session-form.jsx`` keeps the one helper the New session overlay still renders.

``ui/components/new-session-form.jsx`` once held the console's create-session form (``SharedNewSessionForm``, FD2). The shell's ``new-session`` overlay (``NV_CreateSessionOverlay``
in ``console/nv-overlays.jsx``) replaced it and nothing renders it any more: the script was loaded, ``window.SharedNewSessionForm`` was never read, and only comments named it. It drew
7 bare ``field-label`` rows and had six static test files pinning its source text. What the overlay DOES use from the file is ``SharedNewSessionSchemaField``, the row of a graph's
``Begin.input_schema`` form; that is all that is left, and these tests keep it that way so a second create form cannot grow beside the overlay again.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
FILE = UI / "components" / "new-session-form.jsx"
OVERLAYS = UI / "components" / "console" / "nv-overlays.jsx"
INDEX = UI / "index.html"
RETIRED = ("SharedNewSessionForm", "SharedNewSessionFileToBase64")


def _ui_sources() -> list[Path]:
    return [p for p in sorted(UI.rglob("*")) if p.suffix in {".jsx", ".js", ".html"} and "vendor" not in p.relative_to(UI).parts]


def test_the_retired_form_is_named_nowhere_in_the_ui() -> None:
    """No definition, no ``window`` export, and no comment that points a reader at a form that is not there."""
    found = {rel: name for p in _ui_sources() for name in RETIRED if name in p.read_text(encoding="utf-8") for rel in [p.relative_to(UI).as_posix()]}
    assert not found, found


def test_the_file_defines_only_the_graph_input_field() -> None:
    src = FILE.read_text(encoding="utf-8")
    assert re.findall(r"^function (\w+)\(", src, re.M) == ["SharedNewSessionSchemaField"]
    assert re.findall(r"^window\.(\w+) =", src, re.M) == ["SharedNewSessionSchemaField"]


def test_the_overlay_renders_the_graph_input_field_that_keeps_the_file() -> None:
    src = OVERLAYS.read_text(encoding="utf-8")
    start = src.index("function NV_CreateSessionOverlay(")
    assert "<SharedNewSessionSchemaField" in src[start:src.index("\nfunction ", start)]


def test_the_file_is_still_loaded_before_the_overlays() -> None:
    tag = 'src="components/new-session-form.jsx"'
    assert INDEX.read_text(encoding="utf-8").count(tag) == 1
    html = INDEX.read_text(encoding="utf-8")
    assert html.index(tag) < html.index('src="components/console/nv-overlays.jsx"')
