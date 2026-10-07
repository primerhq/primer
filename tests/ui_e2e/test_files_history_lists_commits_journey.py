"""Journey: Files > History lists the workspace's turn commits and opens one (console review 2026-10-08, C-026).

Both Files sidebars read ``commits.data.items`` while ``GET /v1/workspaces/{wid}/log`` answers ``{"commits": [...]}``, so History
always said "No turn commits yet." and the commit-diff tab was unreachable from it.

A local workspace gets a state-repo commit only when an agent turn writes, which this lane has no model for, so the log route is
answered in the browser with the API's own response shape: ``{"commits": [CommitInfo...]}`` (the key and the fields are pinned
server-side by tests/api/test_workspaces.py and primer/model/workspace.py). Everything else, the console, the sidebars and the
diff tab, is the real thing.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_mobile_tab
from tests.ui_e2e._studio_helpers import files_list, open_studio

pytestmark = smk("SMK-UI-06", status="partial")

SHA = "c" * 40
LOG_BODY = {
    "commits": [
        {"sha": SHA, "subject": "turn[1] write notes.md", "committed_at": "2026-10-08T10:00:00Z", "workspace_id": "w",
         "session_id": "sess-c026", "agent_id": "operator", "op": "write", "tool": "files__write", "call_id": "call-1",
         "files": [{"path": "notes.md", "additions": 3, "deletions": 0, "binary": False}]},
    ]
}


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


def _answer_the_log(page: Page) -> None:
    page.route("**/v1/workspaces/*/log*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(LOG_BODY),
    ))


@pytest.mark.ui_e2e
def test_the_history_lists_the_workspace_commits_and_a_click_opens_its_diff(
    page: Page, base_url: str, console_url: str,
) -> None:
    wid = _a_workspace_id(base_url)
    _answer_the_log(page)
    open_studio(page, console_url, wid)
    files_list(page)
    page.get_by_test_id("nv-file-history").click()

    commit = page.get_by_test_id(f"nv-commit:{SHA}")
    expect(commit).to_be_visible(timeout=10_000)
    expect(commit).to_contain_text("turn[1] write notes.md")
    expect(page.get_by_text("No turn commits yet.")).to_have_count(0)

    commit.click()
    expect(page.get_by_test_id(f"nv-tg-tab:diff:{SHA}")).to_be_visible(timeout=10_000)


@pytest.mark.ui_e2e
def test_the_mobile_files_tab_history_lists_the_commits_too(
    page: Page, base_url: str, console_url: str,
) -> None:
    wid = _a_workspace_id(base_url)
    _answer_the_log(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{console_url}#/w/{wid}")
    open_mobile_tab(page, console_url, "files")
    page.get_by_test_id("nv-mob-files-history").click()

    expect(page.get_by_test_id(f"nv-mob-commit:{SHA}")).to_be_visible(timeout=10_000)
    expect(page.get_by_text("No turn commits yet.")).to_have_count(0)
