"""The product topbar (revamp spec section 3).

The 2026-08-23 audit found the topbar flat-rendering every registry
verb as a link strip that overflowed off-screen. The registry is still
the source of pointer affordances (dual-render rule), but the RENDER
is now: brand/workspace, a search field that opens the palette, a
health dot, one "Open..." menu over the topbar-surface verbs, and a
user menu carrying Toggle Theme and Sign Out.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "ui" / "components" / "shell" / "sh-doc-host.jsx"
).read_text(encoding="utf-8")


def test_flat_verb_strip_gone():
    assert "sh-topbar-verbs" not in SRC, (
        "the junk-drawer strip is retired; verbs render in the menu"
    )


def test_search_affordance_opens_palette():
    m = re.search(r'data-testid="shell-topbar-search"[\s\S]{0,400}', SRC)
    assert m, "search affordance missing"
    assert "openPalette" in m.group(0)


def test_open_menu_renders_from_registry():
    m = re.search(r'data-testid="shell-topbar-menu"[\s\S]{0,600}', SRC)
    assert m, "Open... menu missing"
    assert 'forSurface("topbar")' in m.group(0)


def test_user_menu_has_theme_and_signout():
    m = re.search(r'data-testid="shell-topbar-user"[\s\S]{0,900}', SRC)
    assert m, "user menu missing"
    assert "theme.toggle" in m.group(0)
    assert "/v1/auth/logout" in m.group(0)
