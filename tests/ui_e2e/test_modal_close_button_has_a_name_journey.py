"""Journey: a dialog's close button has a name (console review C-003, found by the runtime sweep).

The shared ``Modal`` drew its close control as an icon with no text, so every dialog of the console had one unnamed button. In the real New graph dialog the button is found by the role
"button" named "Close", exactly once, and pressing it closes the dialog.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")


@pytest.mark.ui_e2e
def test_the_new_graph_dialog_has_one_close_button_named_close_and_it_closes_the_dialog(page: Page, console_url: str) -> None:
    open_legacy_route(page, console_url, "graphs")
    page.get_by_role("button", name="New graph").first.click()
    modal = page.locator(".modal").first
    expect(modal).to_be_visible(timeout=10_000)

    close = modal.get_by_role("button", name="Close", exact=True)
    expect(close).to_have_count(1)
    close.click()
    expect(page.locator(".modal")).to_have_count(0, timeout=5_000)
