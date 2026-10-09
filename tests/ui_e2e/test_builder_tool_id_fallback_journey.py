"""Journey: in the real graph builder a tool step can be given a tool when the tool catalogue cannot be loaded (board ticket 01a11e4e-7115).

``GET /v1/tools/catalogue`` is made to fail with a 503. The builder used to show "No tools match." (the words of an empty search) and give no way to set the tool. Now the step's picker says the
list could not be loaded with the server's words, the tool's id can be typed (and the JSON tab, which shows the draft, has it), "Try again" asks for the list again and, once the request
succeeds, the picker is the list it always was.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests._support.smk import smk
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")


def _seed(base_url: str, graph_id: str) -> None:
    nodes = [
        {"kind": "begin", "id": "begin"},
        {"kind": "tool_call", "id": "check", "tool_id": "workspaces__list_files", "arguments": {"path": "."}},
        {"kind": "end", "id": "done", "output_template": ""},
    ]
    edges = [{"kind": "static", "from_node": "begin", "to_node": "check"}, {"kind": "static", "from_node": "check", "to_node": "done"}]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "tool id fallback probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text


@pytest.mark.ui_e2e
def test_a_tool_id_can_be_typed_while_the_catalogue_is_down(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"tool-id-{unique_suffix}"
    _seed(base_url, graph_id)
    state = {"down": True, "calls": 0}

    def catalogue(route: Route) -> None:
        state["calls"] += 1
        if state["down"]:
            route.fulfill(status=503, content_type="application/problem+json", body=json.dumps({"title": "Service Unavailable", "status": 503, "detail": "the tool service is down"}))
        else:
            route.continue_()

    page.route("**/v1/tools/catalogue*", catalogue)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        page.locator('[data-testid="gb-outline-row"][data-node-id="check"]').click()

        alert = page.get_by_test_id("gb-tool-catalogue-error")
        expect(alert).to_be_visible(timeout=15_000)
        expect(alert).to_contain_text("the tool service is down")
        expect(page.get_by_text("No tools match.")).to_have_count(0)

        box = page.get_by_label("Tool id")
        expect(box).to_have_value("workspaces__list_files")
        box.fill("workspaces__read_file")
        gb.expect_dirty(page)
        page.locator('[data-testid="gb-json-tab"]').click()
        spec = json.loads(page.get_by_test_id("graph-import-spec").input_value())
        assert next(n for n in spec["nodes"] if n["id"] == "check")["tool_id"] == "workspaces__read_file"
        page.locator(".modal").get_by_role("button", name="Cancel", exact=True).click()

        # the request succeeds on the next try: the picker is the list again
        state["down"] = False
        page.get_by_test_id("gb-tool-catalogue-retry").click()
        expect(page.get_by_test_id("gb-tool-catalogue-error")).to_have_count(0, timeout=15_000)
        expect(page.get_by_label("Search tools")).to_be_visible()
        assert state["calls"] >= 2
    finally:
        page.unroute("**/v1/tools/catalogue*")
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")
