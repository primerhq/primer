"""Journey: on a phone, a deep-linked section fills the screen, and Providers, System settings and Log out are reachable
(lead sweep M1, review ADM-29 and ADM-30).

390x844. Before: ``#/w/<wid>?overlay=agents`` opened the More tab with the profile card, the theme toggle and four health cards
stacked ABOVE the Agents list (about 1300px down, below the fold); there was no way to sign out, no System settings and no
Providers entry; and ``?view=platform:agents`` left the shell on Inbox.
"""

from __future__ import annotations

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

    page.get_by_test_id("nv-mob-system-back").click()
    expect(page.get_by_test_id("nv-mobile-panel:more")).to_be_visible(timeout=10_000)


@pytest.mark.ui_e2e
def test_providers_opens_from_the_more_tab(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    _open_more(page, console_url)
    page.get_by_test_id("nv-mob-setting:providers").click()
    expect(page.get_by_test_id("nv-overlay:providers")).to_be_visible(timeout=10_000)
