"""``ui/`` uses no React transition and no Suspense (re-review of #773).

The Register overlay's frame store (``PC_makeFrameStore`` in ``ui/components/providers/register-overlay.jsx``) is written while a page RENDERS: ``PC_FramePublisher``
puts the page's parts in the store in its render body, so that the slots in the dialog, which come after the page in the overlay's markup, draw them in the same pass.
That is only sound while a render is never abandoned between the write and the commit. A transition (``startTransition``, ``useTransition``), a deferred value
(``useDeferredValue``) and a Suspense boundary are the ways React 18 abandons or replays a render: one could run the write and then be thrown away, leaving the store
holding the parts of a render that never committed. Nothing in ``ui/`` uses them today; this holds the line, so that the first use is a decision made on purpose (read
"the shell's slots" in register-overlay.jsx first), not an accident.

The vendored libraries are not ours: the scan skips ``*.min.js`` (React itself names all four).
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.ui.test_focus_layers_are_traps import strip_comments

UI = Path(__file__).resolve().parents[2] / "ui"
CONCURRENT = re.compile(r"\b(startTransition|useTransition|useDeferredValue|Suspense)\b")
SUFFIXES = {".js", ".jsx", ".html"}


def _sources() -> list[Path]:
    return [p for p in sorted(UI.rglob("*")) if p.is_file() and p.suffix in SUFFIXES and not p.name.endswith(".min.js")]


def _uses(text: str) -> list[str]:
    """The concurrent-rendering APIs a source names outside its comments."""
    return sorted(set(CONCURRENT.findall(strip_comments(text))))


def test_ui_uses_no_transitions_or_suspense() -> None:
    found = {p.relative_to(UI).as_posix(): _uses(p.read_text(encoding="utf-8")) for p in _sources()}
    found = {path: apis for path, apis in found.items() if apis}
    assert found == {}, (
        "ui/ uses an API that lets React abandon or replay a render, and the Register overlay's frame store is written while a page renders "
        "(register-overlay.jsx, 'the shell's slots'): " + repr(found)
    )


def test_the_scan_covers_the_overlay_and_the_page_that_loads_it() -> None:
    names = {p.relative_to(UI).as_posix() for p in _sources()}
    assert "components/providers/register-overlay.jsx" in names
    assert "components/provider-form.jsx" in names
    assert "index.html" in names
    assert "foundation/use-resource.js" in names
    assert not any(n.startswith("vendor/") and n.endswith(".min.js") for n in names), "the vendored libraries are skipped"
    assert len(names) > 100, "a scan of a handful of files would pass for the wrong reason"


def test_the_scan_names_each_api_and_ignores_comments() -> None:
    assert _uses("React.startTransition(() => setOpen(true));") == ["startTransition"]
    assert _uses("const [pending, go] = React.useTransition();") == ["useTransition"]
    assert _uses("const shown = React.useDeferredValue(query);") == ["useDeferredValue"]
    assert _uses("<React.Suspense fallback={null}><Page /></React.Suspense>") == ["Suspense"]
    assert _uses("import { startTransition, Suspense } from 'react';") == ["Suspense", "startTransition"]
    assert _uses("// a render may not be abandoned: no startTransition here\n/* nor Suspense */\nconst x = 1;") == []
    assert _uses("const startTransitionCount = 1; const mySuspenseLevel = 2;") == [], "whole words only"
