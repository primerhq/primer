"""Journey: the phone's Spaces list reads as a list, clears its button and can be searched (console review C-037).

At 390px the Spaces tab drew workspace and session titles centred (a <button> defaults to centred text) while their chevrons and dots sat at the edges,
let the floating "+" cover the right end of the last rows, said nothing about a session beyond an attention dot, and had no way to reach the command
palette, which is the console's only search, without a keyboard chord.

A workspace with 14 never-started sessions is created through the API (no model is called), so the list is longer than the screen and every row
has a state (Waiting). The real console is driven and measured; nothing in it is mocked.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_mobile_tab
from tests.ui_e2e.test_trace_sidebar_journey import _seed

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}
SESSIONS = 14
STATE_WORDS = ("Running", "Waiting", "Parked", "Paused", "Ready", "Ended")


@pytest.fixture(scope="module")
def seeded_workspace(base_url: str, mock_llm_lan, tmp_path_factory) -> str:
    """One workspace with 14 named, never-started sessions, created once for the module (a session costs an API round trip each)."""
    _registry, mock_base_url = mock_llm_lan
    ids = _seed(base_url, mock_base_url, uuid.uuid4().hex[:8], tmp_path_factory.mktemp("phone-spaces"))
    wid = ids["workspace"]
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        for i in range(SESSIONS):
            r = client.post(f"/v1/workspaces/{wid}/sessions", json={
                "binding": {"kind": "agent", "agent_id": ids["agent"]}, "name": f"phone-list-{i:02d}", "auto_start": False,
            })
            assert r.status_code == 201, f"seed session {i}: {r.status_code} {r.text}"
    return wid


@pytest.fixture
def spaces(page: Page, console_url: str, seeded_workspace: str) -> tuple[Page, str]:
    wid = seeded_workspace
    page.set_viewport_size(PHONE)
    open_mobile_tab(page, console_url, "spaces")
    page.get_by_test_id(f"nv-mob-ws:{wid}").click()
    expect(page.get_by_text("phone-list-00")).to_be_visible(timeout=20_000)
    return page, wid


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_titles_are_left_aligned(spaces: tuple[Page, str]) -> None:
    page, wid = spaces
    for testid in (f"nv-mob-ws:{wid}", "nv-mob-session:"):
        row = page.locator(f"[data-testid^='{testid}']").first
        assert row.evaluate("el => getComputedStyle(el).textAlign") in ("left", "start"), testid


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_every_session_row_says_what_state_it_is_in(spaces: tuple[Page, str]) -> None:
    page, _wid = spaces
    rows = page.locator("[data-testid^='nv-mob-session:']")
    assert rows.count() >= SESSIONS
    missing = [rows.nth(i).inner_text().replace("\n", " | ") for i in range(rows.count())
               if not any(w in rows.nth(i).inner_text() for w in STATE_WORDS)]
    assert not missing, f"rows with no state word: {missing}"


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_the_last_row_scrolls_clear_of_the_floating_button(spaces: tuple[Page, str]) -> None:
    page, _wid = spaces
    # The very last row of the whole tree (a workspace row, or a session row of the last expanded workspace), with the list scrolled to the end of
    # its own scroll container (the nearest scrollable ancestor of that row).
    last_row = page.get_by_test_id("nv-mob-ws-tree").locator("button").last
    last_row.evaluate(
        """el => {
          let n = el.parentElement;
          while (n && !(n.scrollHeight > n.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(n).overflowY))) n = n.parentElement;
          if (n) n.scrollTop = n.scrollHeight;
        }"""
    )
    page.wait_for_timeout(300)
    last = last_row.bounding_box()
    fab = page.get_by_role("button", name="Create session").bounding_box()
    assert last and fab
    overlaps = not (last["x"] + last["width"] <= fab["x"] or fab["x"] + fab["width"] <= last["x"]
                    or last["y"] + last["height"] <= fab["y"] or fab["y"] + fab["height"] <= last["y"])
    assert not overlaps, f"the last row {last} is under the button {fab}"


@pytest.mark.ui_e2e
@pytest.mark.timeout(120)
def test_a_search_button_opens_the_palette_and_is_tap_sized(spaces: tuple[Page, str]) -> None:
    page, _wid = spaces
    search = page.get_by_test_id("nv-mob-search")
    expect(search).to_be_visible(timeout=5_000)
    box = search.bounding_box()
    assert box and min(box["width"], box["height"]) >= 44, box
    search.click()
    expect(page.get_by_test_id("nv-palette")).to_be_visible(timeout=5_000)
