"""Journey: deleting a session from the rail's right-click menu closes its tab (a pin, from the review of PR 501).

The review asked whether the rail's Delete leaves the session's tab open on a document that answers 404 (the overflow menu's Delete closes it
through the document's ``onDeleted``). It does not: the tab goes, a background one too, so nothing was fixed. This journey keeps it that way.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.mock_llm import Rule
from tests.ui_e2e._scripted_session import seed_scripted_agent, start_session
from tests.ui_e2e._shell_helpers import session_row
from tests.ui_e2e._studio_helpers import open_session_in_studio


@pytest.mark.ui_e2e
@pytest.mark.timeout(150)
def test_deleting_from_the_rail_closes_the_sessions_tab(
    page: Page, base_url: str, console_url: str, mock_llm_lan, tmp_path: Path,
):
    registry, mock_base_url = mock_llm_lan
    ids = seed_scripted_agent(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path, prefix="rdel")
    registry.register(ids["model_name"], [Rule(emit_text="hello")])
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        sid = start_session(client, ids, "hello", auto_start=False)
        other = start_session(client, ids, "hello", auto_start=False)
    open_session_in_studio(page, console_url, ids["workspace"], sid)
    tab = page.get_by_test_id(f"nv-tg-tab:session:{sid}")
    expect(tab).to_be_visible(timeout=20_000)

    # Open the other session on top, so the one we delete is a background tab (the document's own Delete only ever closes the active one).
    session_row(page, other, ids["workspace"]).first.click()
    expect(page.get_by_test_id(f"nv-tg-tab:session:{other}")).to_be_visible(timeout=20_000)

    session_row(page, sid, ids["workspace"]).first.click(button="right")
    menu = page.get_by_test_id(f"nv-rail-session-menu:{sid}")
    expect(menu).to_be_visible(timeout=10_000)
    menu.get_by_text("Delete", exact=True).click()
    page.get_by_test_id("dialog-confirm").click()
    expect(page.locator(".toast", has_text="Session deleted")).to_be_visible(timeout=10_000)

    expect(tab).to_have_count(0, timeout=10_000)
    expect(page.get_by_test_id(f"nv-tg-tab:session:{other}")).to_be_visible()
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        assert client.get(f"/v1/sessions/{sid}").status_code == 404
        assert client.get(f"/v1/sessions/{other}").status_code == 200, "only the deleted session went"
