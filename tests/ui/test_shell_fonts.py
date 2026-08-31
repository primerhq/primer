"""The UI face is IBM Plex Sans (designer identity, new-ui handoff
2026-08-23), self-hosted; mono stays IBM Plex Mono. Sans-serif UI
text goes through the --font-ui token so the face changes in exactly
one place.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def test_font_file_vendored():
    f = ROOT / "ui" / "vendor" / "fonts" / "IBMPlexSans-VF-latin.woff2"
    assert f.is_file() and f.stat().st_size > 10_000


def test_font_face_and_token():
    assert "@font-face" in CSS
    assert "'IBM Plex Sans'" in CSS
    assert '--font-ui: "IBM Plex Sans"' in CSS


def test_jakarta_fully_retired():
    assert "Plus Jakarta Sans" not in CSS
    assert "PlusJakartaSans" not in HTML
    assert not (
        ROOT / "ui" / "vendor" / "fonts" / "PlusJakartaSans-VF-latin.woff2"
    ).exists()


def test_preload():
    assert "IBMPlexSans-VF-latin.woff2" in HTML
