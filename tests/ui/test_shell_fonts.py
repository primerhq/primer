"""The UI face is Plus Jakarta Sans (spec 2026-08-23 section 9).

Mono stays IBM Plex Mono; sans-serif UI text goes through the
--font-ui token so the face changes in exactly one place.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def test_font_file_vendored():
    f = ROOT / "ui" / "vendor" / "fonts" / "PlusJakartaSans-VF-latin.woff2"
    assert f.is_file() and f.stat().st_size > 10_000


def test_font_face_and_token():
    assert "@font-face" in CSS and "Plus Jakarta Sans" in CSS
    assert "--font-ui:" in CSS


def test_no_plex_sans_ui_usage():
    # IBM Plex Sans must not remain as a UI face; Plex Mono stays.
    assert "IBM Plex Sans" not in CSS.replace(
        "IBMPlexSans", ""
    ), "replace IBM Plex Sans font-family usages with var(--font-ui)"


def test_preload():
    assert "PlusJakartaSans-VF-latin.woff2" in HTML
