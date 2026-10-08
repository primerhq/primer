"""Journey: a workspace's Files tree does not list the runtime's own folders (console review 2026-10-08, C-007).

A workspace carries ``.state`` (the state repo) and ``.tmp`` (truncated tool outputs). The tree route hid ``.state`` but not ``.tmp``, so
a new workspace's Files rail showed a collapsed ``.tmp`` and the "This workspace has no files yet" state could never appear. The install
under test runs with auth off, so the caller is treated as an admin, which is the case the route got wrong (a non-admin never saw either).

Nothing is mocked: the real route answers, the real rail draws it.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import files_list, open_studio

pytestmark = smk("SMK-UI-06", status="partial")


def _names(base_url: str, wid: str, **params: str) -> list[str]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        resp = c.get(f"/v1/workspaces/{wid}/files/tree", params=params)
    assert resp.status_code == 200, resp.text
    return [i["name"] for i in resp.json()["items"]]


def _a_workspace_with_runtime_trees(base_url: str) -> str:
    """A workspace whose ``hidden=true`` tree lists ``.tmp``: without one the test would prove nothing, so it fails loudly instead."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        ids = [w["id"] for w in c.get("/v1/workspaces").json()["items"]]
    for wid in ids:
        if ".tmp" in _names(base_url, wid, hidden="true"):
            return wid
    raise AssertionError(f"none of {len(ids)} workspaces has a .tmp folder; the seeded workspace is expected to (provisioning creates it)")


@pytest.mark.ui_e2e
def test_the_default_tree_leaves_the_runtime_folders_out_and_hidden_true_brings_them_back(base_url: str) -> None:
    wid = _a_workspace_with_runtime_trees(base_url)

    shown = _names(base_url, wid)
    assert ".tmp" not in shown and ".state" not in shown, shown
    assert {".tmp"} <= set(_names(base_url, wid, hidden="true"))


@pytest.mark.ui_e2e
def test_the_files_rail_does_not_draw_the_runtime_folders(page: Page, base_url: str, console_url: str) -> None:
    wid = _a_workspace_with_runtime_trees(base_url)
    user_files = _names(base_url, wid)

    open_studio(page, console_url, wid)
    files_list(page)

    expect(page.get_by_test_id("nv-files")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-file:.tmp")).to_have_count(0)
    expect(page.get_by_test_id("nv-file:.state")).to_have_count(0)
    if not user_files:
        expect(page.get_by_text("This workspace has no files yet.")).to_be_visible()
