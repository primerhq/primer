"""Journey: the console opens the default workspace (prime), lands a first login in its main session, and never offers to delete it (pivot P2 Tasks 13, 14).

Against the real ui_e2e server: its default workspace is whichever row ``GET /v1/workspaces`` flags ``is_default`` (``prime`` on a fresh install,
``primer`` on one that predates it), read through ``default_wid``, never named. A first load with no address lands on it, on a desktop and on a
phone; its rail menu has Settings and no Delete; its settings overlay has no Destroy tab, and a link to that tab shows why; and it holds the
``agents.md`` the ensure pass seeds.

The first-login open: the console opens the user's main session as a tab only when ``POST /v1/workspaces/{id}/main_session`` answers
``created: true``. This server runs with auth disabled, so every request is the one synthetic user, and the harness makes that user's main session
before any console load (``main_session_exists``, conftest), so every other journey loads with ``created: false``. A fresh user cannot exist here, so
``test_a_first_login_opens_the_main_session_once_and_a_reload_does_not`` plays one: the server's real answer passes through, with ``created`` set
true on the first load only.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import boot_mobile_shell, default_wid, open_shell

PHONE = {"width": 390, "height": 844}
_MAIN_SESSION = re.compile(r".*/v1/workspaces/[^/?]+/main_session$")


@pytest.mark.ui_e2e
def test_first_load_selects_the_default_workspace(page: Page, console_url: str) -> None:
    wid = default_wid(console_url)
    page.goto("about:blank")  # the page fixture already loaded the console: a fresh document, with no address
    page.goto(console_url)
    expect(page.get_by_test_id(f"nv-rail-ws:{wid}")).to_be_visible(timeout=20_000)
    # Not a first login (the harness made the main session), so nothing else opens and the address names only the workspace.
    expect(page).to_have_url(f"{console_url}#/w/{wid}", timeout=20_000)


@pytest.mark.ui_e2e
def test_a_first_login_opens_the_main_session_once_and_a_reload_does_not(page: Page, console_url: str) -> None:
    wid = default_wid(console_url)
    answers: list[dict] = []

    def play_a_first_login(route) -> None:
        response = route.fetch()
        body = response.json()
        answers.append(body)
        route.fulfill(response=response, json={**body, "created": True} if len(answers) == 1 else body)

    page.route(_MAIN_SESSION, play_a_first_login)
    page.goto("about:blank")  # the page fixture already loaded the console: a fresh document, with no address
    page.goto(console_url)

    tabs = page.locator('[data-testid^="nv-tg-tab:session:"]')
    expect(tabs).to_have_count(1, timeout=30_000)
    assert len(answers) == 1, answers
    sid = answers[0]["id"]
    assert answers[0]["created"] is False, "the harness made the main session before any console load"
    assert answers[0]["name"] == "main" and re.fullmatch(rf"sess-{re.escape(wid)}-main-\d+", sid), sid
    expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_have_attribute("data-active", "true")
    expect(page).to_have_url(f"{console_url}#/w/{wid}?doc=session:{sid}", timeout=20_000)

    # Close it, so the working set the reload restores holds no tab, and load again with no address: the answer says created false.
    page.get_by_test_id(f"nv-tg-tab-close:session:{sid}").click()
    expect(tabs).to_have_count(0, timeout=10_000)
    page.goto("about:blank")
    with page.expect_response(_MAIN_SESSION, timeout=30_000):
        page.goto(console_url)
    expect(page.get_by_test_id(f"nv-rail-ws:{wid}")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(1_500)  # the answer has come; give an open that should not happen the time to show
    expect(tabs).to_have_count(0)
    expect(page).to_have_url(f"{console_url}#/w/{wid}")
    assert [a["created"] for a in answers] == [False, False] and answers[1]["id"] == sid


@pytest.mark.ui_e2e
def test_the_default_workspace_rail_menu_has_no_delete(page: Page, console_url: str) -> None:
    wid = default_wid(console_url)
    open_shell(page, console_url, wid)
    page.get_by_test_id(f"nv-rail-ws:{wid}").click(button="right")
    expect(page.get_by_test_id(f"nv-rail-workspace-menu:{wid}")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id(f"nv-rail-ws-menu-settings:{wid}")).to_be_visible()
    expect(page.get_by_test_id(f"nv-rail-ws-menu-new-session:{wid}")).to_be_visible()
    expect(page.get_by_test_id(f"nv-rail-ws-menu-delete:{wid}")).to_have_count(0)


@pytest.mark.ui_e2e
def test_the_default_workspace_settings_have_no_destroy(page: Page, console_url: str) -> None:
    wid = default_wid(console_url)
    open_shell(page, console_url, wid)
    # The settings overlay's section slot is its tab: a link that names the Destroy tab (hash assignment queues the hashchange the shell reads).
    page.evaluate("(h) => { window.location.hash = h; }", f"#/w/{wid}?overlay=workspaces:destroy:{wid}")
    expect(page.get_by_test_id("nv-overlay:workspaces")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("ws-destroy-reserved")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("ws-destroy-reserved")).to_contain_text("This is the default workspace")
    expect(page.get_by_test_id("workspace-tab:config")).to_be_visible()
    expect(page.get_by_test_id("workspace-tab:destroy")).to_have_count(0)
    expect(page.get_by_text("Destroy workspace", exact=True)).to_have_count(0)


@pytest.mark.ui_e2e
def test_prime_holds_its_agents_md(base_url: str, console_url: str) -> None:
    wid = default_wid(console_url)
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.get(f"/v1/workspaces/{wid}/files/info", params={"path": "agents.md"})
    assert r.status_code == 200, r.text


@pytest.mark.ui_e2e
def test_phone_layout_opens_the_default_workspace(page: Page, console_url: str) -> None:
    wid = default_wid(console_url)
    page.set_viewport_size(PHONE)
    page.goto("about:blank")
    boot_mobile_shell(page, f"{console_url}#/")
    expect(page).to_have_url(f"{console_url}#/w/{wid}", timeout=20_000)
