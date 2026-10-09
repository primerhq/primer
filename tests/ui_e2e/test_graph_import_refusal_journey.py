"""Journey: a pasted graph spec with a field of the wrong type is refused with a message, and the builder keeps its draft (review of #633).

``{"nodes": [...], "description": {"x": 1}}`` passed the top-level shape check, and the builder then drew the object as a React child: it threw, the console has no error boundary, the root
unmounted and the unsaved draft was lost. In the real Import spec modal the spec is now refused with one message, the modal stays open, the builder is still on screen with the same steps,
and a good spec still loads.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")


@pytest.mark.ui_e2e
def test_a_spec_of_the_wrong_type_is_refused_and_the_builder_keeps_its_draft(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"imp-refuse-{unique_suffix}"
    nodes = [{"kind": "begin", "id": "begin"}, {"kind": "end", "id": "end", "output_template": ""}]
    edges = [{"kind": "static", "from_node": "begin", "to_node": "end"}]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "import refusal probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(2, timeout=15_000)

        page.locator('[data-testid="gb-json-tab"]').click()
        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=10_000)
        spec = modal.get_by_test_id("graph-import-spec")

        spec.fill(json.dumps({"nodes": [{"kind": "begin", "id": "begin"}], "edges": [], "description": {"x": 1}}))
        modal.get_by_role("button", name="Load into editor").click()
        expect(modal.get_by_text("wrong type")).to_be_visible(timeout=5_000)
        expect(modal).to_be_visible()
        expect(page.locator(gb.BUILDER)).to_be_visible()   # the console root did not unmount
        expect(rows).to_have_count(2)                      # and the draft is as it was

        # a wrong type deeper in a step (the inspector would have drawn the object as a child) is refused the same way
        spec.fill(json.dumps({"nodes": [{"kind": "begin", "id": "begin"}, {"kind": "agent", "id": "a", "agent_id": {"x": 1}}], "edges": []}))
        modal.get_by_role("button", name="Load into editor").click()
        expect(modal.get_by_text("wrong type")).to_be_visible(timeout=5_000)
        expect(rows).to_have_count(2)

        spec.fill(json.dumps({"description": "imported", "nodes": [{"kind": "begin", "id": "begin"}], "edges": []}))
        modal.get_by_role("button", name="Load into editor").click()
        expect(page.locator(".modal")).to_have_count(0, timeout=5_000)
        expect(rows).to_have_count(1)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")
