"""FB3 — the external-link nav indicator used name="external-link", which
shared.jsx's Icon does not define, so it rendered the default fallback circle.
Assert chrome uses the defined "external" glyph."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CHROME = UI / "components" / "chrome.jsx"
SHARED = UI / "components" / "shared.jsx"


def test_icon_defines_external() -> None:
    assert 'case "external":' in SHARED.read_text(encoding="utf-8")


def test_icon_does_not_define_external_link() -> None:
    # There is no "external-link" case, so referencing it is always a fallback.
    assert 'case "external-link":' not in SHARED.read_text(encoding="utf-8")


def test_chrome_uses_defined_external_icon() -> None:
    src = CHROME.read_text(encoding="utf-8")
    assert 'name="external-link"' not in src
    assert 'name="external"' in src
