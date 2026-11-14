"""Shared navigation for the existing Tools catalogue journeys."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import default_wid, open_view

SLOW = 90_000  # The live catalogue may enumerate slow toolset providers.


def open_tools_page(page: Page, console_url: str) -> None:
    """Wait for the shell before assigning its view hash, retrying once
    if mount-time address normalization wins the first navigation."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-plat-page:tools")
    for attempt in (1, 2):
        open_view(page, console_url, default_wid(console_url), "platform:tools")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise
