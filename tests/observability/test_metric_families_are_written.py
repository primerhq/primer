"""Every metric family declared in ``primer/observability/metrics.py`` has a writer somewhere under ``primer/`` (ticket 01a11324).

Removing the per-session WebSocket route left four ``ws_*`` families declared, unit-tested and exported, with no call site: a scrape
listed their HELP and TYPE lines and never a sample, for as long as nobody looked. This is the guard that stops the next removal leaving the same thing behind. A
family counts as written when its module-level variable name appears in any other ``primer/`` source file (an import, a ``.labels(...)``
call); it is a text search, so it is deliberately simple, and a family wired only through ``getattr`` would need an entry below.

``KNOWN_UNWRITTEN`` is the honest exception list. ``claim_active_count`` is documented in docs/dev/architecture/observability.md as
having no writer. The second test fails when a listed family acquires one, so the list shrinks as soon as the gap is closed instead of
rotting.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import primer.observability.metrics as metrics_module

METRICS = Path(metrics_module.__file__).resolve()
PRIMER = METRICS.parents[1]
KIND_NAMES = {"Counter", "Gauge", "Histogram", "Summary", "Info"}

# Declared, exported and unit-tested, with no call site (observability.md says so). Closing the gap removes the entry.
KNOWN_UNWRITTEN = {"claim_active_count"}


def _declared_families() -> list[str]:
    families = []
    for node in ast.parse(METRICS.read_text(encoding="utf-8")).body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.targets[0], ast.Name)):
            continue
        func = node.value.func
        kind = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if kind in KIND_NAMES:
            families.append(node.targets[0].id)
    return families


def _sources_outside_metrics() -> list[str]:
    return [
        path.read_text(encoding="utf-8")
        for path in PRIMER.rglob("*.py")
        if path != METRICS and "__pycache__" not in path.parts
    ]


def _writers(family: str, sources: list[str]) -> int:
    pattern = re.compile(rf"\b{re.escape(family)}\b")
    return sum(1 for text in sources if pattern.search(text))


def test_the_scan_finds_the_declared_families() -> None:
    # A scan that finds nothing would make the guard below pass vacuously.
    families = _declared_families()
    assert len(families) >= 20, f"expected the module-level families, found {families}"


def test_every_declared_family_has_a_writer_or_is_listed_as_known_unwritten() -> None:
    sources = _sources_outside_metrics()
    dead = [family for family in _declared_families() if family not in KNOWN_UNWRITTEN and _writers(family, sources) == 0]

    assert not dead, (
        f"metric families declared in primer/observability/metrics.py but mentioned nowhere else under primer/: {dead}. "
        "Delete the declaration (and its reset-path copy, its __all__ entry, its tests and its doc lines), or write it from a call site."
    )


def test_a_known_unwritten_family_still_has_no_writer() -> None:
    sources = _sources_outside_metrics()
    written = [family for family in sorted(KNOWN_UNWRITTEN) if _writers(family, sources) > 0]

    assert not written, f"{written} now have a writer: remove them from KNOWN_UNWRITTEN (and fix the observability doc)."
