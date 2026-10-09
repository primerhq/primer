"""Journey: in the real graph builder the text controls can be reached and pressed from the keyboard, and the x buttons have a 24 by 24 px box (board task 01a12124).

"+ Add a path", "Remove this connection" and "Delete this step" were spans with a click handler: no focus, no key. The journey focuses "Add a path to the choice after ..." and presses Enter; opens the
Advanced section from the keyboard (its toggle says it is open) and presses "Delete the step ..." with Enter, which asks first (Keep leaves the step, Delete takes it); and measures the x of a condition
and of a path in the browser: 24 by 24 CSS px (WCAG 2.2 SC 2.5.8). The edge inspector's Remove this connection needs a click on an edge of the canvas and is covered in V8.
"""

from __future__ import annotations

import re

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
            {"conditions": [{"path": "ok", "op": "eq", "value": True}, {"path": "n", "op": "gt", "value": 3}], "to_node": "done"},
            {"conditions": [{"path": "ok", "op": "eq", "value": False}], "to_node": "other"}], "default_to": "other"}},
    ]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "link controls probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text


@pytest.mark.ui_e2e
def test_the_text_controls_are_reachable_from_the_keyboard_and_the_x_buttons_are_24_by_24(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"link-controls-{unique_suffix}"
    _seed(base_url, graph_id)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(4, timeout=15_000)
        page.locator('[data-testid="gb-outline-row"][data-node-id="check"]').click()

        # the x buttons: a box of 24 by 24 CSS px, not the 8 by 13 of the glyph
        for name in ("Branch 1, condition 1: remove", "Branch 1: remove"):
            box = page.get_by_role("button", name=name).bounding_box()
            assert box and box["width"] >= 24 - 0.01 and box["height"] >= 24 - 0.01, (name, box)

        # + Add a path, from the keyboard
        add = page.get_by_role("button", name=re.compile(r"^Add a path to the choice after"))
        expect(add).to_have_count(1)
        add.focus()
        expect(add).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.get_by_role("button", name="Branch 3: remove")).to_be_visible(timeout=10_000)

        # Delete this step, from the keyboard: Advanced opens, the button asks first
        advanced = page.get_by_test_id("gb-advanced-toggle")
        expect(advanced).to_have_attribute("aria-expanded", "false")
        advanced.focus()
        page.keyboard.press("Enter")
        expect(advanced).to_have_attribute("aria-expanded", "true")
        delete = page.get_by_role("button", name=re.compile(r"^Delete the step"))
        delete.focus()
        page.keyboard.press("Enter")
        dialog = page.locator(".modal", has_text="Delete this step?")
        expect(dialog).to_be_visible(timeout=10_000)
        dialog.get_by_role("button", name="Keep", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(rows).to_have_count(4)

        delete.focus()
        page.keyboard.press("Enter")
        expect(dialog).to_be_visible(timeout=10_000)
        dialog.get_by_role("button", name="Delete", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(rows).to_have_count(3)
        expect(page.locator('[data-testid="gb-outline-row"][data-node-id="check"]')).to_have_count(0)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")
