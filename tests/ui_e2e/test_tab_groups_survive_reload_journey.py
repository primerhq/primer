"""Journey: the open tabs survive a reload (console review C-023).

The URL names only the active document, so a reload kept one tab of three. Three sessions are opened as tabs (a double click on a rail row promotes a preview), the page
is reloaded, and the working set must come back with the URL's document still the active one.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_shell, session_row
from tests.ui_e2e.test_trace_sidebar_journey import _seed

pytestmark = smk("SMK-UI-06", status="partial")


def _three_sessions(base_url: str, mock_base_url: str, tmp_path: Path) -> tuple[str, list[str]]:
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path)
    wid = ids["workspace"]
    sids = []
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for i in range(3):
            r = c.post(f"/v1/workspaces/{wid}/sessions", json={
                "binding": {"kind": "agent", "agent_id": ids["agent"]}, "name": f"reload-tab-{i}", "auto_start": False,
            })
            assert r.status_code == 201, r.text
            sids.append(r.json()["id"])
    return wid, sids


def test_three_open_tabs_are_three_open_tabs_after_a_reload(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> None:
    _registry, mock_base_url = mock_llm_lan
    wid, sids = _three_sessions(base_url, mock_base_url, tmp_path)
    open_shell(page, console_url, wid)
    for sid in sids:
        session_row(page, sid, wid).dblclick()
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=10_000)
    expect(page.locator('[data-testid^="nv-tg-tab:session:"]')).to_have_count(3)
    active_before = page.url.split("doc=")[1].split("&")[0].split("#")[0]
    assert active_before == f"session:{sids[-1]}"

    page.reload()

    for sid in sids:
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=20_000)
    expect(page.locator('[data-testid^="nv-tg-tab:session:"]')).to_have_count(3)
    assert f"doc=session:{sids[-1]}" in page.url, "the URL's document is still the active one"


def test_a_link_to_another_document_is_the_active_tab_among_the_restored_ones(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> None:
    """The link is LOADED (about:blank first: a new document, not a same-document hash change) and its document was never stored."""
    _registry, mock_base_url = mock_llm_lan
    wid, sids = _three_sessions(base_url, mock_base_url, tmp_path)
    open_shell(page, console_url, wid)
    for sid in sids[:2]:
        session_row(page, sid, wid).dblclick()
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=10_000)

    page.goto("about:blank")
    page.goto(f"{console_url}#/w/{wid}?doc=session:{sids[2]}")

    for sid in sids:
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=20_000)
    assert f"doc=session:{sids[2]}" in page.url


PHONE = {"width": 390, "height": 844}


def _load_and_settle(page: Page, url: str) -> None:
    """A fresh document, then wait until the auth status has been answered and the shell has had time to act on it: the restore (and whatever else the status
    unlocks) runs right after, so an assertion made before that would pass on a screen that is about to change."""
    page.goto("about:blank")
    with page.expect_response("**/v1/auth/status"):
        page.goto(url)
    page.wait_for_timeout(1_200)


def _store(page: Page) -> dict[str, str]:
    return page.evaluate("() => Object.fromEntries(Object.keys(localStorage).filter(k => k.startsWith('primer.console.tabs.v1:')).map(k => [k, localStorage.getItem(k)]))")


def test_a_phone_loads_its_inbox_not_a_tab_a_desktop_left_behind(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> None:
    """The phone's shell shows any open document full screen and its Back closes the tab: a restored desktop set would take the phone over."""
    _registry, mock_base_url = mock_llm_lan
    wid, sids = _three_sessions(base_url, mock_base_url, tmp_path)
    open_shell(page, console_url, wid)
    for sid in sids:
        session_row(page, sid, wid).dblclick()
    expect(page.locator('[data-testid^="nv-tg-tab:session:"]')).to_have_count(3)

    page.set_viewport_size(PHONE)
    _load_and_settle(page, console_url)

    expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-mobile-panel:inbox")).to_be_visible()
    expect(page.get_by_test_id("nv-mob-screen-back")).to_have_count(0)
    assert "doc=" not in page.url, page.url


def test_using_a_phone_does_not_overwrite_the_desktops_stored_tabs(page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path) -> None:
    """Back on the phone closes the open tab; if that were written, the desktop's three tabs would be gone at the next desktop load."""
    _registry, mock_base_url = mock_llm_lan
    wid, sids = _three_sessions(base_url, mock_base_url, tmp_path)
    open_shell(page, console_url, wid)
    for sid in sids:
        session_row(page, sid, wid).dblclick()
    expect(page.locator('[data-testid^="nv-tg-tab:session:"]')).to_have_count(3)
    before = _store(page)
    assert len(before) == 1

    page.set_viewport_size(PHONE)
    _load_and_settle(page, f"{console_url}#/w/{wid}?doc=session:{sids[0]}")
    expect(page.get_by_test_id("nv-mob-screen-back")).to_be_visible(timeout=20_000)
    page.get_by_test_id("nv-mob-screen-back").click()
    expect(page.get_by_test_id("nv-mobile-panel:inbox")).to_be_visible(timeout=10_000)

    assert _store(page) == before, "the phone changed what the desktop stored"
    page.set_viewport_size({"width": 1366, "height": 768})
    _load_and_settle(page, console_url)
    for sid in sids:
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=20_000)


def _status(username: str) -> dict:
    return {"has_user": True, "authenticated": True, "username": username, "role": "admin", "must_change_password": False,
            "setup_complete": True, "setup_missing": []}


def test_a_refetch_that_turns_out_to_be_another_user_does_not_store_the_first_users_tabs_under_the_second(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
) -> None:
    """alice's window is shown again after bob signed in elsewhere: the visibility refetch of the auth status now says bob. The tabs on screen are alice's."""
    _registry, mock_base_url = mock_llm_lan
    wid, sids = _three_sessions(base_url, mock_base_url, tmp_path)
    who = {"name": "alice"}
    page.route("**/v1/auth/status", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(_status(who["name"]))))
    page.goto("about:blank")
    open_shell(page, console_url, wid)
    for sid in sids[:2]:
        session_row(page, sid, wid).dblclick()
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=10_000)
    assert "primer.console.tabs.v1:alice" in _store(page)

    who["name"] = "bob"
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(1_500)
    session_row(page, sids[2], wid).dblclick()
    expect(page.get_by_test_id(f"nv-tg-tab:session:{sids[2]}")).to_be_visible(timeout=10_000)
    page.wait_for_timeout(500)

    assert "primer.console.tabs.v1:bob" not in _store(page), "bob's key received alice's tabs"
