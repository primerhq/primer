"""At mobile widths, list surfaces render as cards, never a
<table className='tbl'>. Tapping a card reaches a detail view.

RETARGET (US-014): NV_MobileShell now owns every viewport in useViewport's
mobile band (<=639px, this file uses 375px), so the desktop's ?overlay=
legacy routes (open_legacy_route) can no longer reach most of these
surfaces directly - NV_OverlayHost, which resolves ?overlay=, only mounts
inside the desktop branch of nv-shell.jsx. A route whose top-level name IS
a mobile Platform nav id (NV_PLAT_GROUPS in nv-platform.jsx) gets
intercepted before it ever reaches NV_OverlayHost and lands on the More
tab's own generic card-list renderer instead (nv-mobile-shell.jsx's
NV_MobilePlatform) - which never has a table branch to begin with, so the
"no table" guarantee transfers there trivially. The provider-catalog
routes ("providers/*", "ssp", "*/providers") have no NV_PLAT_PAGES entry
(provider-catalog.jsx's class-catalog shape does not fit the generic
{list, card} contract - see nv-platform.jsx's own comment), so they still
fall through to the classic overlay unintercepted; that classic page
component still self-detects mobile width via the same useViewport hook
regardless of which shell mounted it, so "no table" still holds for real,
just through the pre-existing (and already-flaky - US-011b) overlay-mount
path rather than a mobile-native one.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Page, expect  # noqa: E402


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import (
    open_legacy_route,
    open_mobile_platform_nav,
    open_mobile_tab,
)
pytestmark = smk("SMK-UI-01", status="partial")


# legacy route -> mobile Platform nav id (NV_PLAT_GROUPS). Two routes name
# a SECTION of a nav ("workspaces/templates", "channels/rules") that mobile
# has no distinct drill-down for (NV_MobilePlatform is flat per top-level
# nav id) - they land on the same base list as their parent nav, which is
# still a faithful "no table" check for that same underlying page.
MOBILE_PLATFORM_ROUTES = {
    "/workspaces": "workspaces",
    "/workspaces/templates": "workspaces",
    "/agents": "agents",
    "/graphs": "graphs",
    "/knowledge/collections": "collections",
    "/toolsets": "toolsets",
    "/approvals": "approvals",
    "/channels": "channels",
    "/channels/rules": "channels",
    "/harnesses": "harnesses",
}

# US-011b: the pre-existing ui_e2e overlay-mount race (orig handoff
# known-issue #1), same family as test_mobile_modal_is_sheet.py's mobile
# params and test_agents_create.py's u0007 - confirmed by repeated
# isolation runs during the US-014 triage (2026-08-29): these routes pass
# reliably alone and fail intermittently in the full-file run, with no
# fixed victim. Provider-catalog is simply not intercepted by the mobile
# shell (see module docstring), so it rides the same race as any other
# unmigrated overlay at this viewport.
_MOBILE_OVERLAY_FLAKY = pytest.mark.flaky(reruns=2, reruns_delay=1)
PROVIDER_CATALOG_ROUTES = [
    "/workspaces/providers",
    "/providers/llm",
    "/providers/embedding",
    "/providers/cross_encoder",
    "/ssp",
    "/channels/providers",
]


@pytest.mark.ui_e2e
@pytest.mark.parametrize("route", sorted(MOBILE_PLATFORM_ROUTES))
def test_mobile_no_table_on_platform_nav_route(
    page: Page, console_url: str, route: str
) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_mobile_platform_nav(page, console_url, MOBILE_PLATFORM_ROUTES[route])
    expect(page.locator("table.tbl")).to_have_count(0)


@pytest.mark.ui_e2e
@_MOBILE_OVERLAY_FLAKY
@pytest.mark.parametrize("route", PROVIDER_CATALOG_ROUTES)
def test_mobile_no_table_on_provider_catalog_route(
    page: Page, console_url: str, route: str
) -> None:
    page.set_viewport_size({"width": 375, "height": 812})
    open_legacy_route(page, console_url, route)
    page.wait_for_load_state("domcontentloaded")
    expect(page.locator("table.tbl")).to_have_count(0)


@pytest.mark.ui_e2e
def test_mobile_workspace_row_expands_and_opens_a_session(
    page: Page, console_url: str
) -> None:
    """REWRITE (US-014): "tapping a workspace card enters that workspace,
    URL becomes #/w/<id>" was the classic WorkspacesPage's own behavior.
    NV_MobileShell's Spaces tab has no workspace-detail concept - a
    workspace row is an accordion that expands to its sessions inline
    (nv-mobile-shell.jsx's NV_MobileSpaces); tapping a SESSION row is what
    actually opens something, the full-screen chat surface M2/M3 built.
    That is the real mobile "tap to reach detail" path now.
    """
    page.set_viewport_size({"width": 375, "height": 812})
    open_mobile_tab(page, console_url, "spaces")

    ws_rows = page.locator("[data-testid^='nv-mob-ws:']")
    try:
        ws_rows.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        pytest.skip("no workspaces seeded in this environment")

    # Expand workspace rows until one with a seeded session turns up - a
    # fresh environment may have workspaces with none yet.
    session_row = None
    for i in range(ws_rows.count()):
        ws_rows.nth(i).click()
        candidate = page.locator("[data-testid^='nv-mob-session:']").first
        try:
            candidate.wait_for(state="visible", timeout=3_000)
            session_row = candidate
            break
        except Exception:
            ws_rows.nth(i).click()  # collapse back before trying the next
    if session_row is None:
        pytest.skip("no workspace with a seeded session in this environment")

    session_row.click()
    expect(page.get_by_test_id("nv-mob-session-screen")).to_be_visible(timeout=10_000)
