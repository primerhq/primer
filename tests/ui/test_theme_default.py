"""Light-first theming (spec 2026-08-23 section 9).

No hard-coded dark default: index.html resolves persisted choice, then
system preference, before first paint. The tweaks default is null so an
operator with no saved choice follows the OS. The profile menu's Theme row
offers Light, Dark and System, and the verbs theme.light, theme.dark and
theme.system make the same three choices reachable from the palette
(D3, 2026-10-18; tests/ui/test_profile_menu_theme.py runs them).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
TWEAKS = (ROOT / "ui" / "foundation" / "tweaks.js").read_text(encoding="utf-8")
CHROME = (
    ROOT / "ui" / "components" / "console" / "nv-chrome.jsx"
).read_text(encoding="utf-8")


def test_no_static_dark_attribute():
    tag = re.search(r"<html[^>]*>", HTML)
    assert tag and "data-theme" not in tag.group(0)


def test_bootstrap_script_resolves_theme():
    # Dark-first (designer identity, new-ui handoff): dark unless the
    # OS explicitly prefers light; persisted choice always wins.
    assert "prefers-color-scheme: light" in HTML
    assert "primer.tweaks" in HTML  # reads the persisted store key


def test_tweaks_default_follows_system():
    assert re.search(r"theme:\s*null", TWEAKS), (
        "tweaks theme default must be null (= follow system)"
    )


def test_theme_control_reachable():
    # D3: the profile menu's Theme row offers Light, Dark and System; each button runs through a palette verb id.
    assert 'data-testid="nv-theme-seg"' in CHROME
    for value in ("light", "dark", "system"):
        assert f'data-testid="nv-theme:{value}"' in CHROME and f'data-verb="theme.{value}"' in CHROME
    assert 'setTweak("theme"' in CHROME
    assert 'setAttribute("data-theme"' in CHROME
