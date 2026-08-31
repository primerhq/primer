"""Light-first theming (spec 2026-08-23 section 9).

No hard-coded dark default: index.html resolves persisted choice, then
system preference, before first paint. The tweaks default is null so an
operator with no saved choice follows the OS. A Toggle Theme verb makes
the choice reachable from the palette (and, later, the user menu).
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
    # The profile menu's Dark/Light segment persists the choice and
    # stamps the root attribute (nv chrome, flag day).
    assert 'data-testid="nv-theme-seg"' in CHROME
    assert 'setTweak("theme"' in CHROME
    assert 'setAttribute("data-theme"' in CHROME
