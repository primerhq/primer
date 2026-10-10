"""Journey: focus-holding layers above a dialog are traps too (review of #729, round 2).

The document listener of the focus trap answered a Tab from ``<body>`` (or from a layer that is not a trap) with the first control of the trap BEHIND the layer's scrim, and a following Enter closed the overlay. Two layers
did it: the graph builder's add-step palette in its second stage (the disabled search box drops focus to ``<body>``; Tab+Enter closed the builder and lost the unsaved draft) and the Ctrl+K command palette over the
Create session overlay (Tab went to the overlay's close button behind the palette). The third case is the review's first nit: a focus lost from the MIDDLE of a dialog continues from its place (the bind menu's search
box is removed by Escape; the next Tab goes to the next field, as it did before the trap).
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._shell_helpers import open_legacy_route, open_shell
from tests.ui_e2e.test_escape_closes_one_layer_journey import _seed

_INSIDE = """([sel]) => { const d = document.querySelector(sel), a = document.activeElement; return { inside: !!(d && a && d.contains(a)), body: a === document.body, testid: (a && a.getAttribute && a.getAttribute('data-testid')) || null }; }"""


def _a_workspace_id(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        return client.get("/v1/workspaces").json()["items"][0]["id"]


@pytest.mark.ui_e2e
def test_tab_then_enter_in_the_add_step_second_stage_keeps_the_builder_and_its_draft(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"trap-palette-{unique_suffix}"
    _seed(base_url, graph_id)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        page.locator('[data-testid="gb-outline-row"][data-node-id="check"]').click()
        page.get_by_role("button", name="Branch 1: add a condition").click()          # an unsaved change that would be lost with the overlay
        gb.expect_dirty(page)

        page.locator(gb.OUTLINE_ADD).first.click()
        palette = page.locator(gb.PALETTE)
        expect(palette).to_be_visible(timeout=10_000)
        rows = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=gb-palette-row]')).map((r) => r.getAttribute('data-purpose'))")
        for _ in range(rows.index("tool")):
            page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")                                                   # the second stage: the search box is disabled
        expect(palette.get_by_role("button", name="Add step")).to_be_visible(timeout=10_000)
        page.wait_for_timeout(400)                                                     # Chromium hands the focus of a disabled control to <body> within about 50 ms

        state = page.evaluate(_INSIDE, ["[data-testid=gb-palette]"])
        assert state["inside"], f"the second stage left the focus outside the palette: {state}"
        for key in ("Tab", "Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab"):
            page.keyboard.press(key)
            state = page.evaluate(_INSIDE, ["[data-testid=gb-palette]"])
            assert state["inside"], f"{key} in the second stage went behind the palette's scrim: {state}"
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")
        expect(page.get_by_test_id("nv-overlay:graphs")).to_be_visible()               # the builder is still there ...
        gb.expect_dirty(page)                                                          # ... with its draft
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            client.delete(f"/v1/graphs/{graph_id}")


@pytest.mark.ui_e2e
def test_tab_in_the_command_palette_over_an_overlay_stays_in_the_palette(page: Page, base_url: str, console_url: str) -> None:
    open_shell(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-rail-create-session").click()
    expect(page.get_by_role("dialog").first).to_be_visible(timeout=10_000)
    page.get_by_test_id("nv-ns-name").focus()
    page.keyboard.press("Control+k")
    palette = page.get_by_test_id("nv-palette")
    expect(palette).to_be_visible(timeout=10_000)
    page.wait_for_timeout(300)
    for key in ["Tab"] * 3 + ["Shift+Tab"] * 5:
        page.keyboard.press(key)
        state = page.evaluate(_INSIDE, ["[data-testid=nv-palette]"])
        assert state["inside"], f"{key} in the command palette went to the overlay behind it: {state}"
    page.keyboard.press("Escape")                                                      # the palette goes, the overlay under it stays
    expect(palette).to_have_count(0, timeout=5_000)
    expect(page.locator(".nv-overlay-panel")).to_have_count(1)


@pytest.mark.ui_e2e
def test_a_focus_lost_from_the_middle_of_the_create_session_overlay_continues_in_place(page: Page, base_url: str, console_url: str) -> None:
    """The bind menu autofocuses its search box and Escape removes it, so focus falls to <body>. The next Tab goes to the NEXT field (the name), not to the dialog's first stop."""
    open_shell(page, console_url, _a_workspace_id(base_url))
    page.get_by_test_id("nv-rail-create-session").click()
    expect(page.get_by_role("dialog").first).to_be_visible(timeout=10_000)
    page.get_by_test_id("nv-ns-bind").click()
    menu = page.get_by_test_id("nv-ns-bind-menu")
    expect(menu).to_be_visible(timeout=5_000)
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0, timeout=5_000)
    page.wait_for_timeout(300)
    state = page.evaluate(_INSIDE, [".nv-overlay-panel"])
    assert state["body"], f"the scenario needs a focus that fell to <body>: {state}"
    page.keyboard.press("Tab")
    state = page.evaluate(_INSIDE, [".nv-overlay-panel"])
    assert state["inside"] and state["testid"] == "nv-ns-name", f"Tab did not continue from where the lost control was: {state}"
