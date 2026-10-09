"""Journey: in the real graph builder a condition, a path and a whole choice between paths can be removed (board tickets 01a11e4e-6fa7 and 01a11e4e-7059).

The step with the choice is selected: its second condition is removed by its named button (the first and the path stay), the second path by its own (the last path of a choice has none that
works), and the choice itself by "Remove this choice" after a confirmation. The JSON tab, which shows the draft, is the witness each time. The retired legacy editor could do all three.
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
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "remove choice probe", "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text


def _draft(page: Page) -> dict:
    page.locator('[data-testid="gb-json-tab"]').click()
    spec = page.get_by_test_id("graph-import-spec")
    expect(spec).to_be_visible(timeout=10_000)
    draft = json.loads(spec.input_value())
    page.locator(".modal").get_by_role("button", name="Cancel", exact=True).click()
    return draft


def _branches(draft: dict) -> list[dict]:
    return next(e for e in draft["edges"] if e["kind"] == "conditional")["router"]["branches"]


@pytest.mark.ui_e2e
def test_a_condition_a_path_and_a_choice_can_each_be_removed(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"remove-choice-{unique_suffix}"
    _seed(base_url, graph_id)
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        page.locator('[data-testid="gb-outline-row"][data-node-id="check"]').click()

        # a path has its own named remove
        expect(page.get_by_role("button", name="Branch 1: remove")).to_be_enabled(timeout=10_000)

        page.get_by_role("button", name="Branch 1, condition 2: remove").click()
        branches = _branches(_draft(page))
        assert [len(b["conditions"]) for b in branches] == [1, 1], branches

        page.get_by_role("button", name="Branch 2: remove").click()
        assert [b["to_node"] for b in _branches(_draft(page))] == ["done"]
        only = page.get_by_role("button", name="Branch 1: remove")
        expect(only).to_have_attribute("aria-disabled", "true")      # still focusable, and described by why
        expect(only).to_have_accessible_description("A choice needs at least one path: remove the whole choice instead.")
        only.click(force=True)                                       # Playwright treats aria-disabled as not clickable: press it anyway
        assert len(_branches(_draft(page))) == 1, "the only path stays"

        # the console's own dialog asks; Keep leaves the choice, Remove takes it (and its "In any other case" link) away
        remove = page.get_by_test_id("gb-remove-choice")
        remove.click()
        dialog = page.locator(".modal", has_text="Remove this choice?")
        expect(dialog).to_be_visible(timeout=10_000)
        expect(dialog).to_contain_text("its 1 path and its 'in any other case' link")
        # (Escape is not pressed here: in the graph's overlay one Escape closes the overlay as well and leaves the dialog on screen; see the PR body.)
        dialog.get_by_role("button", name="Keep", exact=True).click()
        expect(dialog).to_have_count(0)
        assert any(e["kind"] == "conditional" for e in _draft(page)["edges"]), "keeping leaves the choice"

        remove.click()
        expect(dialog).to_be_visible(timeout=10_000)
        dialog.get_by_role("button", name="Remove", exact=True).click()
        expect(dialog).to_have_count(0)
        assert not any(e["kind"] == "conditional" for e in _draft(page)["edges"])
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")
