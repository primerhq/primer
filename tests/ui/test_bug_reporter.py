"""Static checks for the bug reporter (component + vendoring + wiring)."""

from __future__ import annotations

from pathlib import Path


BUG_REPORTER = (
    Path(__file__).resolve().parents[2] / "ui" / "components" / "bug_reporter.jsx"
)
HTML2CANVAS = (
    Path(__file__).resolve().parents[2] / "ui" / "vendor" / "html2canvas.min.js"
)
INDEX = Path(__file__).resolve().parents[2] / "ui" / "index.html"
APP = Path(__file__).resolve().parents[2] / "ui" / "app.jsx"


def test_bug_reporter_component_defined() -> None:
    src = BUG_REPORTER.read_text(encoding="utf-8")
    assert "BG_BugButton" in src


def test_bug_reporter_posts_to_v1_bugs() -> None:
    src = BUG_REPORTER.read_text(encoding="utf-8")
    assert "/bugs" in src


def test_bug_reporter_uses_html2canvas() -> None:
    src = BUG_REPORTER.read_text(encoding="utf-8")
    assert "html2canvas" in src


def test_bug_reporter_has_button_testid() -> None:
    src = BUG_REPORTER.read_text(encoding="utf-8")
    assert "bug-report-btn" in src


def test_bug_reporter_floats_bottom_left() -> None:
    src = BUG_REPORTER.read_text(encoding="utf-8")
    assert "bottom" in src and "left" in src
    assert (
        'position: "fixed"' in src
        or "position:'fixed'" in src
        or "position: 'fixed'" in src
    )


def test_html2canvas_vendored() -> None:
    assert HTML2CANVAS.exists(), (
        "html2canvas must be vendored at ui/vendor/html2canvas.min.js"
    )
    # File size sanity — should be well over 30KB (1.4.1 is ~200KB).
    size = HTML2CANVAS.stat().st_size
    assert size > 30_000, (
        f"html2canvas vendor file too small ({size} bytes) — "
        "download likely failed"
    )


def test_index_loads_vendor_and_reporter() -> None:
    src = INDEX.read_text(encoding="utf-8")
    assert "html2canvas.min.js" in src
    assert "bug_reporter.jsx" in src


def test_app_renders_bug_button() -> None:
    src = APP.read_text(encoding="utf-8")
    assert "BG_BugButton" in src
