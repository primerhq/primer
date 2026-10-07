"""Journey: Files > History lists the workspace's turn commits and opens one (console review 2026-10-08, C-026).

Both Files sidebars read ``commits.data.items`` while ``GET /v1/workspaces/{wid}/log`` answers ``{"commits": [...]}``, so History
always said "No turn commits yet." and the commit-diff tab was unreachable from it. This goes through the REAL server: a freshly
created local workspace already has its first state-repo commit, so nothing is mocked and the response shape is the API's own.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_mobile_tab
from tests.ui_e2e._studio_helpers import files_list, open_studio

pytestmark = smk("SMK-UI-06", status="partial")


def _seed_workspace(base_url: str, suffix: str) -> tuple[dict[str, str], str]:
    """A local workspace on a container-internal path (the U0106 pattern); returns the ids and the first commit's sha."""
    ids = {"wp": f"wp-c026-{suffix}", "tpl": f"tpl-c026-{suffix}", "workspace": ""}
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": ids["wp"], "provider": "local", "config": {"kind": "local", "root_path": f"/tmp/c026-{suffix}"},
        })
        assert r.status_code == 201, f"wp: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": ids["tpl"], "description": "c026 tpl", "provider_id": ids["wp"], "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"tpl: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": ids["tpl"]})
        assert r.status_code == 201, f"ws: {r.text}"
        ids["workspace"] = r.json()["id"]
        log = c.get(f"/v1/workspaces/{ids['workspace']}/log", params={"limit": 50, "with_files": 1})
        if log.status_code >= 500:
            pytest.skip(f"the server cannot read the workspace's state repo here: {log.status_code}")
        assert log.status_code == 200, log.text
        commits = log.json()["commits"]
        assert commits, "a fresh workspace has its first state-repo commit"
    return ids, commits[0]["sha"]


def _cleanup(base_url: str, ids: dict[str, str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/workspaces/{ids['workspace']}" if ids.get("workspace") else None,
            f"/v1/workspace_templates/{ids['tpl']}",
            f"/v1/workspace_providers/{ids['wp']}",
        ):
            if url is not None:
                try:
                    c.delete(url)
                except Exception:  # noqa: BLE001
                    pass


@pytest.mark.ui_e2e
def test_the_history_lists_the_workspace_commits_and_a_click_opens_its_diff(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    ids, first_sha = _seed_workspace(base_url, unique_suffix)
    try:
        open_studio(page, console_url, ids["workspace"])
        files_list(page)
        page.get_by_test_id("nv-file-history").click()

        commit = page.get_by_test_id(f"nv-commit:{first_sha}")
        expect(commit).to_be_visible(timeout=10_000)
        expect(page.get_by_text("No turn commits yet.")).to_have_count(0)

        commit.click()
        expect(page.get_by_test_id(f"nv-tg-tab:diff:{first_sha}")).to_be_visible(timeout=10_000)
    finally:
        _cleanup(base_url, ids)


@pytest.mark.ui_e2e
def test_the_mobile_files_tab_history_lists_the_commits_too(
    page: Page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    ids, first_sha = _seed_workspace(base_url, unique_suffix)
    try:
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{console_url}#/w/{ids['workspace']}")
        open_mobile_tab(page, console_url, "files")
        page.get_by_test_id("nv-mob-files-history").click()

        expect(page.get_by_test_id(f"nv-mob-commit:{first_sha}")).to_be_visible(timeout=10_000)
        expect(page.get_by_text("No turn commits yet.")).to_have_count(0)
    finally:
        _cleanup(base_url, ids)
