"""Journey: the workspace menu and the profile menu close on Escape and give focus back to their button (console review 2026-10-08, C-027).

Neither menu had a keyboard handler: Escape left them open (a click outside was the only way), the open workspace menu intercepted clicks aimed at the rail
behind it, and a keyboard user who tabbed out of a menu left it hanging open. Now Escape closes the open menu and returns focus to the button that opened it, and
focus moving to something outside the menu and its button closes it too. Escape with no menu open does nothing here.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._studio_helpers import open_studio


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


def _focused_testid(page: Page) -> str | None:
    return page.evaluate("document.activeElement && document.activeElement.getAttribute('data-testid')")


@pytest.mark.ui_e2e
def test_escape_closes_the_workspace_menu_and_returns_focus_to_the_chip(page: Page, base_url: str, console_url: str) -> None:
    open_studio(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-ws-chip").click()
    expect(page.get_by_test_id("nv-ws-menu")).to_be_visible()

    page.keyboard.press("Escape")

    expect(page.get_by_test_id("nv-ws-menu")).to_have_count(0)
    assert _focused_testid(page) == "nv-ws-chip", "focus goes back to the button that opened the menu"


@pytest.mark.ui_e2e
def test_escape_closes_the_profile_menu_and_returns_focus_to_the_avatar(page: Page, base_url: str, console_url: str) -> None:
    open_studio(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-profile-btn").click()
    expect(page.get_by_test_id("nv-profile-menu")).to_be_visible()

    page.keyboard.press("Escape")

    expect(page.get_by_test_id("nv-profile-menu")).to_have_count(0)
    assert _focused_testid(page) == "nv-profile-btn"


@pytest.mark.ui_e2e
def test_tabbing_out_of_the_workspace_menu_closes_it(page: Page, base_url: str, console_url: str) -> None:
    open_studio(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-ws-chip").click()
    menu = page.get_by_test_id("nv-ws-menu")
    expect(menu).to_be_visible()

    # Tab walks into the menu's rows (they follow the chip) and, past the last one, to the next control, which is outside it.
    for _ in range(40):
        page.keyboard.press("Tab")
        if menu.count() == 0:
            break
    expect(menu).to_have_count(0)


@pytest.mark.ui_e2e
def test_escape_with_no_menu_open_leaves_the_page_alone(page: Page, base_url: str, console_url: str) -> None:
    open_studio(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-search-btn").focus()
    page.keyboard.press("Escape")
    assert _focused_testid(page) == "nv-search-btn", "Escape with nothing open does not move focus"
    expect(page.get_by_test_id("nv-ws-menu")).to_have_count(0)
    expect(page.get_by_test_id("nv-profile-menu")).to_have_count(0)


@pytest.mark.ui_e2e
def test_a_click_outside_still_closes_the_menu(page: Page, base_url: str, console_url: str) -> None:
    open_studio(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-ws-chip").click()
    expect(page.get_by_test_id("nv-ws-menu")).to_be_visible()
    page.mouse.click(700, 500)
    expect(page.get_by_test_id("nv-ws-menu")).to_have_count(0)
