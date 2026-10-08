"""Journey: a dialog takes focus, keeps it, and gives it back (console review 2026-10-08, C-028).

Before: the console's own overlays (the Create session overlay and every other ``?overlay=`` page) had no ``role="dialog"``, no
``aria-modal`` and no focus handling. Right after opening, focus was still on the "+" behind the scrim; ten Tab presses walked through
the inbox rows, the workspace tree and the Files toolbar; Escape closed the overlay and focus landed on an unrelated button. The mobile
bottom sheet had the dialog role and nothing else. The shared ``Modal`` (every form modal and the confirm/prompt dialogs) already had a
trap and a restore; the overlays and the sheet now use the same one.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_mobile_tab, open_shell

_FOCUS_IN_DIALOG = """() => {
  const d = document.querySelector('[role=dialog]');
  return !!d && d.contains(document.activeElement) && document.activeElement !== d;
}"""
_FOCUS_STAYS_IN_DIALOG = """() => {
  const d = document.querySelector('[role=dialog]');
  return !!d && d.contains(document.activeElement);
}"""


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return c.get("/v1/workspaces").json()["items"][0]["id"]


@pytest.mark.ui_e2e
def test_a_console_overlay_takes_focus_traps_it_and_gives_it_back(page: Page, base_url: str, console_url: str) -> None:
    open_shell(page, console_url, _a_workspace_id(base_url))
    opener = page.get_by_test_id("nv-rail-create-session")
    opener.focus()
    opener.click()

    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog).to_have_attribute("aria-modal", "true")
    labelled_by = dialog.get_attribute("aria-labelledby")
    assert labelled_by, "the dialog has no accessible name"
    assert page.evaluate("(id) => (document.getElementById(id) || {}).textContent", labelled_by) == "New session"

    # Focus moved INTO the dialog (not onto the dialog box itself, and not left on the "+" behind the scrim).
    page.wait_for_function(_FOCUS_IN_DIALOG, timeout=5_000)
    for n in range(14):
        page.keyboard.press("Tab")
        assert page.evaluate(_FOCUS_STAYS_IN_DIALOG), f"Tab {n + 1} left the dialog"
    for n in range(4):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate(_FOCUS_STAYS_IN_DIALOG), f"Shift+Tab {n + 1} left the dialog"

    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
    expect(opener).to_be_focused()


@pytest.mark.ui_e2e
def test_the_phones_create_session_sheet_takes_focus_and_gives_it_back(page: Page, base_url: str, console_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    open_mobile_tab(page, console_url, "spaces")
    fab = page.get_by_role("button", name="Create session")
    fab.focus()
    fab.click()

    sheet = page.get_by_role("dialog")
    expect(sheet).to_be_visible(timeout=10_000)
    expect(sheet).to_have_attribute("aria-modal", "true")
    page.wait_for_function(_FOCUS_IN_DIALOG, timeout=5_000)
    for n in range(12):
        page.keyboard.press("Tab")
        assert page.evaluate(_FOCUS_STAYS_IN_DIALOG), f"Tab {n + 1} left the sheet"

    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
    expect(fab).to_be_focused()


@pytest.mark.ui_e2e
def test_a_confirm_dialog_still_traps_and_restores_focus(page: Page, base_url: str, console_url: str) -> None:
    """The shared Modal's trap moved into the shared hook; this pins that it still behaves."""
    wid = _a_workspace_id(base_url)
    open_shell(page, console_url, wid)
    page.evaluate("() => { window.__confirm = window.confirmDialog({ title: 'Are you sure', message: 'Really?', danger: true }); }")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog).to_have_attribute("aria-label", re.compile("Are you sure"))
    page.wait_for_function(_FOCUS_IN_DIALOG, timeout=5_000)
    for n in range(6):
        page.keyboard.press("Tab")
        assert page.evaluate(_FOCUS_STAYS_IN_DIALOG), f"Tab {n + 1} left the confirm dialog"
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
