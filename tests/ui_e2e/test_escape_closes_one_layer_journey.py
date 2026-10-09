"""Journey: one Escape closes ONE layer of the console, the top-most (board task 01a12147).

The overlay panel and ``Modal`` each added their own window ``keydown`` listener, so one Escape closed every layer that was listening. In the graph builder a confirm dialog opened over the builder's
overlay: the Escape closed the builder (an unsaved draft lost) and left the dialog on screen. ``ui/foundation/escape-stack.js`` answers Escape for the top-most layer only.

The first journey is that case in the real builder: a draft with an unsaved change, the "Remove this choice?" dialog, Escape. The dialog goes, the builder and the draft stay, and the second Escape closes the
overlay. The second is a form modal over a Platform overlay, the same rule for ``Modal`` itself.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")


def _seed(base_url: str, graph_id: str) -> None:
    nodes = [
        {"kind": "begin", "id": "begin"},
        {"kind": "tool_call", "id": "check", "tool_id": "workspaces__list_files", "arguments": {"path": "."}},
        {"kind": "end", "id": "done", "output_template": ""},
        {"kind": "end", "id": "other", "output_template": ""},
    ]
    edges = [
        {"kind": "static", "from_node": "begin", "to_node": "check"},
        {"kind": "conditional", "from_node": "check", "router": {"kind": "json_path", "branches": [
            {"conditions": [{"path": "ok", "op": "eq", "value": True}, {"path": "n", "op": "gt", "value": 3}], "to_node": "done"}], "default_to": "other"}},
    ]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "escape probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text


@pytest.mark.ui_e2e
def test_escape_in_a_confirm_dialog_over_the_graph_builder_closes_the_dialog_and_not_the_builder(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"escape-{unique_suffix}"
    _seed(base_url, graph_id)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        page.locator('[data-testid="gb-outline-row"][data-node-id="check"]').click()
        page.get_by_role("button", name="Branch 1: add a condition").click()      # an unsaved change that would be lost with the overlay
        gb.expect_dirty(page)

        page.get_by_test_id("gb-remove-choice").click()
        dialog = page.locator(".modal", has_text="Remove this choice?")
        expect(dialog).to_be_visible(timeout=10_000)

        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0, timeout=5_000)
        expect(page.locator(gb.BUILDER)).to_be_visible()                           # the builder is still there ...
        gb.expect_dirty(page)                                                      # ... with its draft
        expect(page.get_by_test_id("nv-overlay:graphs")).to_be_visible()
        assert page.get_by_role("button", name="Branch 1: add a condition").count() == 1, "the choice was not removed"

        page.keyboard.press("Escape")                                              # the second Escape is the overlay's
        expect(page.get_by_test_id("nv-overlay:graphs")).to_have_count(0, timeout=5_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")


@pytest.mark.ui_e2e
def test_escape_in_a_form_modal_over_a_platform_overlay_closes_the_modal_first(page: Page, console_url: str) -> None:
    open_legacy_route(page, console_url, "agents")
    overlay = page.get_by_test_id("nv-overlay:agents")
    expect(overlay).to_be_visible(timeout=15_000)
    page.get_by_role("button", name="New agent").first.click()
    modal = page.locator(".modal")
    expect(modal).to_be_visible(timeout=10_000)

    page.keyboard.press("Escape")
    expect(modal).to_have_count(0, timeout=5_000)
    expect(overlay).to_be_visible()

    page.keyboard.press("Escape")
    expect(overlay).to_have_count(0, timeout=5_000)
