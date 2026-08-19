"""The flag day is only executable once nothing else reads what it deletes.

Task 28's grep-clean gate and Task 29's facade contract both assume this
sweep already ran. Asserting it separately means a missed module fails
HERE, with a list, instead of failing as a FileNotFoundError at collection
time in the `test` lane or as a dead hash route in the `ui-e2e` lane.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
SELF = Path(__file__).resolve()

DOOMED = [
    "chrome.jsx", "dashboard.jsx", "studio.jsx", "studio-activity.jsx",
    "studio-center.jsx", "studio-palette.jsx", "studio-settings.jsx",
    "studio-sidebar.jsx", "studio-terminal.jsx", "components/studio/",
    "components/studio2/", "foundation/router.js",
]


def _modules() -> list[Path]:
    out = list((TESTS / "ui").rglob("test_*.py"))
    out += list((TESTS / "ui_e2e").rglob("test_*.py"))
    return sorted(p for p in out if p.resolve() != SELF)


def test_no_ui_test_reads_a_doomed_path() -> None:
    offenders = {
        str(p.relative_to(ROOT)): [
            t for t in DOOMED if t in p.read_text(encoding="utf-8")
        ]
        for p in _modules()
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, offenders


def test_every_ui_e2e_module_navigates_through_the_facade() -> None:
    """Mirror of S9's programme gate, asserted here so P5 discovers it.

    A goto that never mentions ``console_url`` is not console navigation
    at all (the docs-embed spike loads a static page), so the rule binds
    to modules that actually drive the console.
    """
    strays = []
    for p in sorted((TESTS / "ui_e2e").rglob("test_*.py")):
        src = p.read_text(encoding="utf-8")
        if "page.goto(" not in src or "console_url" not in src:
            continue
        if "_shell_helpers" in src or "_studio_helpers" in src:
            continue
        strays.append(str(p.relative_to(ROOT)))
    assert not strays, strays


def test_no_ui_e2e_module_drives_a_retired_hash_route() -> None:
    """A facade import is necessary but not sufficient: a module can
    import the helpers and still hand-roll a route the shell no longer
    serves. The shell answers exactly two URL shapes, "#/" and "#/w/...",
    so any other "#/<page>" in a goto is a route that died with the
    router table."""
    from tests.ui_e2e._shell_helpers import LEGACY_ROUTE_OVERLAYS

    # Only what a goto actually navigates to counts: the same route
    # named in a docstring or a page.route() interception is prose or a
    # network stub, not navigation.
    calls = re.compile(r"page\.goto\(([^)]*)\)", re.S)

    offenders: dict[str, list[str]] = {}
    for p in sorted((TESTS / "ui_e2e").rglob("test_*.py")):
        src = p.read_text(encoding="utf-8")
        hits = set()
        for call in calls.findall(src):
            for legacy in LEGACY_ROUTE_OVERLAYS:
                if f"#/{legacy}" in call:
                    hits.add(legacy)
        if hits:
            offenders[str(p.relative_to(ROOT))] = sorted(hits)
    assert not offenders, offenders
