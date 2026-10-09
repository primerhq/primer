"""Journey: a branch condition whose value is an object or a list of objects is not corrupted by typing in its box, and what is saved is what was typed (board ticket 01a11e4e-8910).

The builder drew an object value as ``[object Object]``, and the first keystroke replaced the object by a string. In the real builder the step with the conditions is selected, each value box
shows the JSON, a character typed into it stores nothing, SAYS so in an alert and turns Save off while another edit is pending (round 2 of #679: Save used to PUT the old value and lose
the typed text), a valid edit stores the new value, and after Save the graph the SERVER returns has an object (a dict) and a list of objects, not strings.
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

_OBJECT = {"a": 1}
_OBJECTS = [{"k": 1}]


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
            {"conditions": [{"path": "ok", "op": "eq", "value": _OBJECT}], "to_node": "done"},
            {"conditions": [{"path": "ok", "op": "in", "value": _OBJECTS}], "to_node": "other"}], "default_to": "other"}},
    ]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "branch value probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text


def _stored(base_url: str, graph_id: str) -> list[dict]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get(f"/v1/graphs/{graph_id}")
        assert r.status_code == 200, r.text
        edge = next(e for e in r.json()["edges"] if e["kind"] == "conditional")
        return [b["conditions"][0] for b in edge["router"]["branches"]]


@pytest.mark.ui_e2e
def test_typing_into_an_object_value_does_not_replace_it_and_the_saved_graph_has_what_was_typed(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"branch-value-{unique_suffix}"
    _seed(base_url, graph_id)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        page.locator('[data-testid="gb-outline-row"][data-node-id="check"]').click()
        obj = page.get_by_label("Branch 1, condition 1: value")
        objs = page.get_by_label("Branch 2, condition 1: value")
        alert = page.get_by_test_id("gb-branch-value-error")
        expect(obj).to_be_visible(timeout=10_000)
        expect(obj).to_have_value(json.dumps(_OBJECT, separators=(",", ":")))       # not "[object Object]"
        expect(objs).to_have_value(json.dumps(_OBJECTS, separators=(",", ":")))     # not "[object Object]" either

        obj.press("End")
        obj.press_sequentially("x")
        expect(obj).to_have_value('{"a":1}x')
        expect(obj).to_have_attribute("aria-invalid", "true")
        expect(alert).to_have_count(1)
        expect(alert).to_contain_text("Not JSON yet")
        gb.expect_clean(page)                                                        # nothing was stored: the draft is as it was
        obj.fill('{"a":1}')                                                          # back to what is stored: nothing pending, no alert
        expect(alert).to_have_count(0)
        expect(obj).not_to_have_attribute("aria-invalid", "true")

        # another edit is pending: the half-typed text must stop Save (it used to PUT the old value and lose the text)
        objs.fill('[{"k":2}]')
        expect(gb.save_button(page)).to_be_enabled(timeout=10_000)
        obj.press("End")
        obj.press_sequentially("x")
        expect(alert).to_have_count(1)
        expect(gb.save_button(page)).to_be_disabled()
        obj.fill('{"a":2}')
        expect(alert).to_have_count(0)
        expect(gb.save_button(page)).to_be_enabled()

        # a list of objects under "is one of" is guarded just the same
        objs.press("End")
        objs.press_sequentially("x")
        expect(objs).to_have_attribute("aria-invalid", "true")
        expect(gb.save_button(page)).to_be_disabled()
        objs.fill('[{"k":3}]')
        expect(alert).to_have_count(0)
        expect(gb.save_button(page)).to_be_enabled()

        # the JSON tab is the draft; the SERVER's graph is what Save stored
        gb.save_button(page).click()
        gb.expect_clean(page, timeout=20_000)
        first, second = _stored(base_url, graph_id)
        assert first["value"] == {"a": 2} and isinstance(first["value"], dict), first
        assert second["value"] == [{"k": 3}] and all(isinstance(m, dict) for m in second["value"]), second
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")
