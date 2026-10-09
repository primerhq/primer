"""Journey: a branch condition whose value is an object is not corrupted by typing in its box (board ticket 01a11e4e-8910).

The builder drew an object value as ``[object Object]``, and the first keystroke replaced the object by a string. In the real builder the step with the condition is selected, its value box
shows the JSON, a character typed into it leaves the draft clean (nothing was stored), and finishing a valid edit stores the new object, which the JSON tab then shows.
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

_VALUE = {"a": 1}


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
            {"conditions": [{"path": "ok", "op": "eq", "value": _VALUE}], "to_node": "done"}], "default_to": "other"}},
    ]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "branch value probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text


@pytest.mark.ui_e2e
def test_typing_into_an_object_value_does_not_replace_it(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"branch-value-{unique_suffix}"
    _seed(base_url, graph_id)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        page.locator('[data-testid="gb-outline-row"][data-node-id="check"]').click()
        box = page.get_by_label("Branch 1, condition 1: value")
        expect(box).to_be_visible(timeout=10_000)
        expect(box).to_have_value(json.dumps(_VALUE, separators=(",", ":")))   # not "[object Object]"

        box.press("End")
        box.press_sequentially("x")
        expect(box).to_have_value('{"a":1}x')
        expect(box).to_have_attribute("aria-invalid", "true")
        gb.expect_clean(page)                                                  # nothing was stored: the draft is as it was

        box.fill('{"a":2}')
        expect(box).not_to_have_attribute("aria-invalid", "true")
        gb.expect_dirty(page)
        page.locator('[data-testid="gb-json-tab"]').click()
        spec = json.loads(page.get_by_test_id("graph-import-spec").input_value())
        condition = next(e for e in spec["edges"] if e["kind"] == "conditional")["router"]["branches"][0]["conditions"][0]
        assert condition["value"] == {"a": 2}, condition
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")
