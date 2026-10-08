"""Journey: the Create session dialog says which workspace it creates in and wears no developer verb chip (lead sweep L3).

The uiv2 mockup annotates a panel with its palette verb ("verb: Create Session"); that note shipped as a mono-spaced pill beside the
title. And the dialog never named the workspace the new session goes into.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_overlay, open_shell


def _first_workspace(base_url: str) -> tuple[str, str]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"], items[0].get("name") or items[0]["id"]


@pytest.mark.ui_e2e
def test_the_dialog_names_its_workspace_and_has_no_verb_chip(page: Page, base_url: str, console_url: str) -> None:
    wid, name = _first_workspace(base_url)
    open_shell(page, console_url, wid)
    open_overlay(page, console_url, wid, "new-session")

    panel = page.get_by_test_id("nv-overlay:new-session")
    expect(panel.get_by_test_id("nv-ns-workspace")).to_contain_text(name)
    expect(panel.locator(".nv-verb-chip")).to_have_count(0)
    expect(panel).not_to_contain_text("verb:")
