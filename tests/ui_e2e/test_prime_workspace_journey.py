"""Journey: the console opens the default workspace (prime) and never offers to delete it (pivot P2 Tasks 13 and 14).

Against the real ui_e2e server: its default workspace is whichever row ``GET /v1/workspaces`` flags ``is_default`` (``prime`` on a fresh install,
``primer`` on one that predates it), read through ``default_wid``, never named. A first load with no address lands on it, on a desktop and on a
phone; its rail menu has Settings and no Delete; its settings overlay has no Destroy tab, and a link to that tab shows why; and it holds the
``agents.md`` the ensure pass seeds.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import boot_mobile_shell, default_wid, open_shell

PHONE = {"width": 390, "height": 844}


@pytest.mark.ui_e2e
def test_first_load_selects_the_default_workspace(page: Page, console_url: str) -> None:
    wid = default_wid(console_url)
    page.goto("about:blank")  # the page fixture already loaded the console: a fresh document, with no address
    page.goto(console_url)
    expect(page.get_by_test_id(f"nv-rail-ws:{wid}")).to_be_visible(timeout=20_000)
    expect(page).to_have_url(f"{console_url}#/w/{wid}", timeout=20_000)


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
