"""Journey: a focus parked on an element that is no tab stop cannot walk out of a dialog (board task 01a122c8-33c6, measured in the review of #705).

``useFocusTrap`` (``ui/foundation/focus-trap.js``) wrapped a Tab only when focus WAS the last tab stop (or outside the dialog), and a Shift+Tab only when it WAS the first. A focus that sits on an element
that is no tab stop (a ``tabindex="-1"`` heading a removal hands focus to, a status line, the dialog box itself after a click) AFTER the last tab stop made the next Tab leave for the page behind the scrim, and
one BEFORE the first made the next Shift+Tab leave for the page in front of it. The test parks focus on such an element in the console's Create session overlay (a real dialog, the one
``test_overlay_focus_journey.py`` drives) and presses the keys.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_shell

_PARK = """([where]) => {
  const d = document.querySelector('[role=dialog]');
  const el = document.createElement('div');
  el.tabIndex = -1;
  el.id = 'parked-' + where;
  el.textContent = 'parked ' + where;
  if (where === 'tail') d.appendChild(el); else d.insertBefore(el, d.firstChild);
  el.focus();
  return document.activeElement === el;
}"""
_STATE = """() => {
  const d = document.querySelector('[role=dialog]');
  const stops = window.primerApi.focusablesOf(d);
  const now = document.activeElement;
  return { inside: d.contains(now), first: now === stops[0], last: now === stops[stops.length - 1], stops: stops.length, id: now.id || null };
}"""


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return c.get("/v1/workspaces").json()["items"][0]["id"]


def _open_the_create_session_overlay(page: Page, base_url: str, console_url: str) -> None:
    open_shell(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-rail-create-session").click()
    expect(page.get_by_role("dialog")).to_be_visible(timeout=10_000)
    page.wait_for_function("() => { const d = document.querySelector('[role=dialog]'); return !!d && d.contains(document.activeElement) && document.activeElement !== d; }", timeout=5_000)


@pytest.mark.ui_e2e
def test_tab_from_an_element_after_the_last_tab_stop_wraps_to_the_first(page: Page, base_url: str, console_url: str) -> None:
    _open_the_create_session_overlay(page, base_url, console_url)
    assert page.evaluate(_PARK, ["tail"]) is True
    assert page.evaluate(_STATE)["stops"] >= 2
    page.keyboard.press("Tab")
    state = page.evaluate(_STATE)
    assert state["inside"], "a Tab from an element after the last tab stop left the dialog"
    assert state["first"], f"it stayed inside but did not wrap to the first tab stop: {state}"


@pytest.mark.ui_e2e
def test_shift_tab_from_an_element_before_the_first_tab_stop_wraps_to_the_last(page: Page, base_url: str, console_url: str) -> None:
    _open_the_create_session_overlay(page, base_url, console_url)
    assert page.evaluate(_PARK, ["head"]) is True
    page.keyboard.press("Shift+Tab")
    state = page.evaluate(_STATE)
    assert state["inside"], "a Shift+Tab from an element before the first tab stop left the dialog"
    assert state["last"], f"it stayed inside but did not wrap to the last tab stop: {state}"
