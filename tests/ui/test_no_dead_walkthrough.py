"""The first-run walkthrough module is gone because nothing used it (console review 2026-10-08, C-040).

``ui/foundation/shell-walkthrough.js`` defined ``SH_WALKTHROUGH_STEPS`` and ``SH_walkthroughState`` for a seeded operator turn that was
never built; no component called either, so the module was loaded on every page for nothing and the docs promised a first-run path that
does not exist. The mockup mentions the walkthrough only in the copy of a welcome toast. If a first-run walkthrough is ever built, it
starts from a design, not from this stub.
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
DOCS = Path(__file__).resolve().parents[2] / "docs" / "dev"


def test_the_module_does_not_exist_and_is_not_loaded() -> None:
    assert not (UI / "foundation" / "shell-walkthrough.js").exists()
    assert "shell-walkthrough" not in (UI / "index.html").read_text(encoding="utf-8")


def test_nothing_under_ui_refers_to_the_removed_identifiers() -> None:
    hits = []
    for path in UI.rglob("*"):
        if path.suffix not in {".js", ".jsx", ".html"} or "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in ("SH_WALKTHROUGH", "SH_walkthrough"):
            if needle in text:
                hits.append(f"{path.relative_to(UI)}: {needle}")
    assert not hits, hits


def test_the_developer_docs_do_not_list_the_module() -> None:
    assert "shell-walkthrough" not in (DOCS / "subsystems" / "ui-pages.md").read_text(encoding="utf-8")
