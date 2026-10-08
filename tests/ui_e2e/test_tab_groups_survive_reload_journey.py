"""Journey: the open tabs survive a reload (console review C-023).

The URL names only the active document, so a reload kept one tab of three. Three sessions are opened as tabs (a double click on a rail row promotes a preview), the page
is reloaded, and the working set must come back with the URL's document still the active one.
"""

from __future__ import annotations

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
    _registry, mock_base_url = mock_llm_lan
    wid, sids = _three_sessions(base_url, mock_base_url, tmp_path)
    open_shell(page, console_url, wid)
    for sid in sids[:2]:
        session_row(page, sid, wid).dblclick()
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=10_000)

    page.goto(f"{console_url}#/w/{wid}?doc=session:{sids[2]}")
    page.reload()

    for sid in sids:
        expect(page.get_by_test_id(f"nv-tg-tab:session:{sid}")).to_be_visible(timeout=20_000)
    assert f"doc=session:{sids[2]}" in page.url
