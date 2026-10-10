"""Journey: on a phone, a deep-linked section fills the screen, and Providers, System settings and Log out are reachable
(lead sweep M1, review ADM-29 and ADM-30).

390x844. Before: ``#/w/<wid>?overlay=agents`` opened the More tab with the profile card, the theme toggle and four health cards
stacked ABOVE the Agents list (about 1300px down, below the fold); there was no way to sign out, no System settings and no
Providers entry; and ``?view=platform:agents`` left the shell on Inbox.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_mobile_shell

PHONE = {"width": 390, "height": 844}


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


def _open_more(page: Page, console_url: str) -> None:
    open_mobile_shell(page, console_url)
    page.get_by_role("tab", name="More").click()
    expect(page.get_by_test_id("nv-mobile-panel:more")).to_be_visible(timeout=10_000)


@pytest.mark.ui_e2e
def test_a_deep_linked_section_fills_the_screen_and_back_returns_to_the_tab(
    page: Page, base_url: str, console_url: str,
) -> None:
    wid = _a_workspace_id(base_url)
    page.set_viewport_size(PHONE)
    page.goto(f"{console_url}#/w/{wid}?overlay=agents")

    expect(page.get_by_test_id("nv-mob-plat-page:agents")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("nv-mob-profile")).to_have_count(0)
    expect(page.get_by_test_id("nv-mob-plat-page:agents")).to_be_in_viewport()   # not 1300px down

    page.get_by_test_id("nv-mob-plat-back").click()
    expect(page.get_by_test_id("nv-mob-plat-sections")).to_be_visible()
    expect(page.get_by_test_id("nv-mob-profile")).to_be_visible()


@pytest.mark.ui_e2e
def test_a_platform_view_link_opens_that_section_on_the_phone(page: Page, base_url: str, console_url: str) -> None:
    wid = _a_workspace_id(base_url)
    page.set_viewport_size(PHONE)
    page.goto(f"{console_url}#/w/{wid}?view=platform:agents")
    expect(page.get_by_test_id("nv-mob-plat-page:agents")).to_be_visible(timeout=15_000)


@pytest.mark.ui_e2e
def test_the_palette_opens_the_section_again_after_back(page: Page, base_url: str, console_url: str) -> None:
    """At ``?view=platform:agents`` the URL keeps naming the section after Back. Picking an Agents row from the palette (which
    navigates to the same platform view again) must open the section again, not leave the section list on screen. The
    view-object half of this (a repeated goView with the same name and nav) is pinned in V8 by
    ``tests/ui/test_mobile_more_reachability.py``; the palette also sets an overlay, so this journey would pass without that
    half and is the user-visible check, not its mutant killer."""
    wid = _a_workspace_id(base_url)
    page.set_viewport_size(PHONE)
    page.goto(f"{console_url}#/w/{wid}?view=platform:agents")
    expect(page.get_by_test_id("nv-mob-plat-page:agents")).to_be_visible(timeout=15_000)
    page.get_by_test_id("nv-mob-plat-back").click()
    expect(page.get_by_test_id("nv-mob-plat-sections")).to_be_visible()

    page.keyboard.press("Control+k")
    palette_input = page.get_by_test_id("nv-palette-input")
    expect(palette_input).to_be_visible(timeout=10_000)
    palette_input.fill("operator")
    # The Agents entity row (text "operator" + tag "agent"), not the wiki page "agents/operator" that also matches the query.
    page.get_by_test_id("nv-palette-row").filter(has_text=re.compile(r"^\s*operator\s*agent\s*$", re.I)).first.click()

    expect(page.get_by_test_id("nv-mob-plat-page:agents")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-mob-profile")).to_have_count(0)


@pytest.mark.ui_e2e
def test_a_back_gesture_does_not_reopen_a_section_the_user_has_left(page: Page, base_url: str, console_url: str) -> None:
    """On a phone nothing cleared ``view=platform:agents`` from the URL, and the shell re-parses the URL into a fresh view object on every
    hashchange or popstate, so an Android back gesture later reopened the Agents section behind whatever the user had moved on to (and
    in-app Back landed on More > Agents). The link is now consumed once it has been acted on."""
    wid = _a_workspace_id(base_url)
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        agents = c.get("/v1/agents", params={"limit": 1}).json()["items"]
        assert agents, "the install has a bootstrap agent"
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={"binding": {"kind": "agent", "agent_id": agents[0]["id"]}, "auto_start": False})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
    page.set_viewport_size(PHONE)
    page.goto(f"{console_url}#/w/{wid}?view=platform:agents")
    expect(page.get_by_test_id("nv-mob-plat-page:agents")).to_be_visible(timeout=15_000)
    expect(page).not_to_have_url(re.compile(r"view=platform"), timeout=10_000)   # consumed: the URL no longer names it
    page.get_by_test_id("nv-mob-plat-back").click()
    expect(page.get_by_test_id("nv-mob-plat-sections")).to_be_visible()
    page.get_by_role("tab", name="Inbox").click()
    expect(page.get_by_test_id("nv-mobile-panel:inbox")).to_be_visible()

    # Open a session (a history entry), then the back gesture (a popstate to the entry before it).
    page.evaluate(
        "(sid) => { const h = window.location.hash; window.location.hash = h + (h.includes('?') ? '&' : '?') + 'doc=session:' + sid; }",
        sid,
    )
    expect(page.get_by_test_id("nv-mob-session-screen")).to_be_visible(timeout=15_000)
    page.go_back()
    page.wait_for_timeout(1_000)
    if page.get_by_test_id("nv-mob-screen-back").count():
        page.get_by_test_id("nv-mob-screen-back").click()
    expect(page.get_by_test_id("nv-mobile-panel:inbox")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-mob-plat-page:agents")).to_have_count(0)


@pytest.mark.ui_e2e
def test_log_out_is_reachable_on_a_phone(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    _open_more(page, console_url)
    calls: list[str] = []

    def fake_logout(route) -> None:
        calls.append(route.request.method)
        route.fulfill(status=204)

    page.route("**/v1/auth/logout", fake_logout)
    page.get_by_test_id("nv-mob-logout").click()
    page.wait_for_timeout(500)
    assert calls == ["POST"], "Log out posts to the logout route"


@pytest.mark.ui_e2e
def test_system_settings_opens_full_screen_and_back_returns(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    _open_more(page, console_url)
    page.get_by_test_id("nv-mob-setting:system").click()
    expect(page.get_by_test_id("nv-mob-system-screen")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-system")).to_be_visible()
    expect(page.get_by_test_id("nv-sys-row:users")).to_be_in_viewport(ratio=1)  # a chip is content-sized, not as wide as the strip

    page.get_by_test_id("nv-mob-system-back").click()
    expect(page.get_by_test_id("nv-mobile-panel:more")).to_be_visible(timeout=10_000)


@pytest.mark.ui_e2e
def test_providers_opens_from_the_more_tab(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    _open_more(page, console_url)
    page.get_by_test_id("nv-mob-setting:providers").click()
    expect(page.get_by_test_id("nv-overlay:providers")).to_be_visible(timeout=10_000)
