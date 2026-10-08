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


_COUNT_FOCUSABLES = "() => window.primerApi.focusablesOf(document.querySelector('[role=dialog]')).length"
_IS = "(el) => document.activeElement === el"


def _assert_the_trap_cycles(page: Page, what: str) -> int:
    """Tab through the dialog once around and a little more: every press stays inside, and ``count`` presses come back to where they
    started (a trap that merely stays inside, or one that lets focus run off the end, cannot do both). Then the same with Shift+Tab."""
    count = page.evaluate(_COUNT_FOCUSABLES)
    assert count >= 2, f"{what}: a dialog with {count} focusable element(s) cannot show a wrap"
    start = page.evaluate_handle("() => document.activeElement")
    for n in range(count):
        page.keyboard.press("Tab")
        assert page.evaluate(_FOCUS_STAYS_IN_DIALOG), f"{what}: Tab {n + 1} of {count} left the dialog"
    assert page.evaluate(_IS, start), f"{what}: {count} Tabs did not wrap back to the element they started from"
    for n in range(2):
        page.keyboard.press("Tab")
        assert page.evaluate(_FOCUS_STAYS_IN_DIALOG), f"{what}: Tab {count + n + 1} (past the wrap) left the dialog"
    for _ in range(2):
        page.keyboard.press("Shift+Tab")
    for n in range(count):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate(_FOCUS_STAYS_IN_DIALOG), f"{what}: Shift+Tab {n + 1} of {count} left the dialog"
    assert page.evaluate(_IS, start), f"{what}: {count} Shift+Tabs did not wrap back to the element they started from"
    return count


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
    assert page.evaluate("(id) => (document.getElementById(id) || {}).textContent", labelled_by) == "Create session"

    # Focus moved INTO the dialog (not onto the dialog box itself, and not left on the "+" behind the scrim).
    page.wait_for_function(_FOCUS_IN_DIALOG, timeout=5_000)
    _assert_the_trap_cycles(page, "the Create session overlay")

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
    _assert_the_trap_cycles(page, "the phone's Create session sheet")

    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
    expect(fab).to_be_focused()


@pytest.mark.ui_e2e
def test_a_confirm_dialog_still_traps_and_restores_focus(page: Page, base_url: str, console_url: str) -> None:
    """The shared Modal's trap moved into the shared hook; this pins that it still behaves."""
    wid = _a_workspace_id(base_url)
    open_shell(page, console_url, wid)
    opener = page.get_by_test_id("nv-rail-create-session")
    opener.focus()
    expect(opener).to_be_focused()
    page.evaluate("() => { window.__confirm = window.confirmDialog({ title: 'Are you sure', message: 'Really?', danger: true }); }")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog).to_have_attribute("aria-label", re.compile("Are you sure"))
    page.wait_for_function(_FOCUS_IN_DIALOG, timeout=5_000)
    _assert_the_trap_cycles(page, "the confirm dialog")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
    expect(opener).to_be_focused()
