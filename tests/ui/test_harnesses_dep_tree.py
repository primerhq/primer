"""Static JSX checks for dependencies panel — Spec A §13."""

from __future__ import annotations

from pathlib import Path


HARNESSES = Path(__file__).resolve().parents[2] / "ui" / "components" / "harnesses.jsx"


def _src() -> str:
    return HARNESSES.read_text(encoding="utf-8")


def test_detail_page_references_dependencies_resolved():
    src = _src()
    assert "dependencies_resolved" in src


def test_detail_page_renders_dep_rows():
    src = _src()
    # Should iterate the list and show name, slug, git_url, ref, resolved_commit
    assert "resolved_commit" in src
    # Slug + git_url are likely rendered too
    assert ".slug" in src or "['slug']" in src


def test_detail_page_section_titled_dependencies():
    src = _src()
    assert "Dependencies" in src
