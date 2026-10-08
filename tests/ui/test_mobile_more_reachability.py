"""The mobile More tab reaches what the desktop reaches (lead sweep M1, review ADM-29 and ADM-30).

Before: a deep-linked platform surface (``?overlay=agents``) opened on the More tab BELOW the profile card, the theme toggle, four
health cards and a PLATFORM header (the list started about 1300px down), a phone could not sign out, open System settings or open
Providers, and ``?view=platform:*`` / ``?view=system:*`` links left the shell on Inbox.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")


def _rows():
    from py_mini_racer import MiniRacer

    start = SHELL.index("function NV_mobileSettingsRows")
    end = SHELL.index("\n}\n", start) + len("\n}\n")
    ctx = MiniRacer()
    ctx.eval(SHELL[start:end])
    return lambda role: json.loads(ctx.eval("JSON.stringify(NV_mobileSettingsRows(" + json.dumps(role) + "))"))


def test_the_settings_rows_follow_the_roles_the_desktop_menu_uses() -> None:
    rows = _rows()
    assert [r["id"] for r in rows("admin")] == ["providers", "system"]
    assert [r["id"] for r in rows(None)] == ["providers", "system"], "an install without roles is not restricted"
    assert [r["id"] for r in rows("restricted")] == ["providers"], "System settings stays hidden from a restricted user, as on desktop"
    assert [r["label"] for r in rows("admin")] == ["Providers", "System settings"]


def test_an_open_section_fills_the_more_tab() -> None:
    more = SHELL[SHELL.index("function NV_MobileMore"):SHELL.index("function NV_MobileShell")]
    assert "navOpen" in more
    assert "onNavChange={" in more, "the platform list tells the tab when a section is open"
    # The dashboard (profile, health cards, settings) is not rendered above an open section.
    assert re.search(r"!navOpen\s*\?", more), "the dashboard is gated on no section being open"
    platform = SHELL[SHELL.index("function NV_MobilePlatform"):SHELL.index("function NV_MobileMore")]
    assert "props.onNavChange" in platform


def test_more_offers_providers_system_settings_and_log_out() -> None:
    more = SHELL[SHELL.index("function NV_MobileMore"):SHELL.index("function NV_MobileShell")]
    assert "NV_mobileSettingsRows(con.role)" in more
    assert 'data-testid={"nv-mob-setting:" + row.id}' in more
    assert 'data-testid="nv-mob-logout"' in SHELL
    assert '"/v1/auth/logout"' in SHELL
    assert 'con.openOverlay("providers"' in SHELL
    assert 'con.goView("system"' in SHELL


def test_a_system_view_opens_full_screen_on_the_phone_with_a_way_back() -> None:
    shell = SHELL[SHELL.index("function NV_MobileShell"):]
    assert 'con.view.name === "system"' in shell
    assert "NV_MobileSystemScreen" in shell
    screen = SHELL[SHELL.index("function NV_MobileSystemScreen"):SHELL.index("function NV_MobileMore")]
    assert 'data-testid="nv-mob-system-screen"' in screen
    assert "window.NV_System" in screen
    assert 'data-testid="nv-mob-system-back"' in screen
    assert 'con.goView("studio")' in screen


def test_a_platform_view_link_lands_on_the_more_tab_with_that_section_open() -> None:
    shell = SHELL[SHELL.index("function NV_MobileShell"):]
    assert 'con.view.name === "platform"' in shell
    effect = shell[shell.index('con.view.name === "platform"') - 400:shell.index('con.view.name === "platform"') + 500]
    assert "setPendingFactSheet(" in effect and 'setActiveTab("more")' in effect


def test_a_link_to_a_whole_section_is_consumed_as_soon_as_the_section_opens() -> None:
    """Left pending until the list had loaded, a Back tap in that first moment re-opened the section: the effect found a pending kind
    that was not the open nav and set it again (the phone journey caught it)."""
    platform = SHELL[SHELL.index("function NV_MobilePlatform"):SHELL.index("function NV_MobileSystemScreen")]
    effect = platform[platform.index("props.pending.kind"):]
    assert "if (!props.pending.id)" in effect[:600]
    assert effect.index("if (!props.pending.id)") < effect.index("if (res.loading) return;")
