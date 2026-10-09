"""Journey: every visible control of the live graph builder has a name, for every kind of step (console review C-003, the graph builder).

A graph with a begin step (and an input schema), an agent with a conditional edge, a sub-graph, a tool call, a fan-out and its workers, a merge and a finish is opened in the real builder and
each step is selected in turn: the inspector's title, its pickers, the schema rows, the branch editor, the fan-out's three ways to split and the tool call's argument controls must each have a
label or an ``aria-label``. The palette ("Add a step") and its search are checked too.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")

_UNNAMED = """els => els.filter(e => e.type !== 'hidden' && e.offsetParent !== null
    && e.labels.length === 0 && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby'))
    .map(e => e.outerHTML.slice(0, 100))"""

_SPLITS = ("One copy per item in a list", "The same step, N times", "Several different steps at once")


def _seed(base_url: str, suffix: str) -> tuple[str, str]:
    child, parent = f"gbn-child-{suffix}", f"gbn-parent-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": child, "description": "child", "nodes": [{"kind": "begin", "id": "begin"}, {"kind": "end", "id": "end", "output_template": ""}],
                                       "edges": [{"kind": "static", "from_node": "begin", "to_node": "end"}]})
        assert r.status_code == 201, r.text
        r = c.post("/v1/graphs", json={
            "id": parent, "description": "every kind of step", "max_iterations": 10,
            "nodes": [
                {"kind": "begin", "id": "begin", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
                {"kind": "agent", "id": "decider", "agent_id": "operator"},
                {"kind": "graph", "id": "sub", "graph_id": child},
                {"kind": "tool_call", "id": "tool", "tool_id": "workspaces__list_files", "arguments": {"path": "."}},
                {"kind": "fan_out", "id": "split", "specs": [
                    {"kind": "broadcast", "target_node_id": "worker", "count": 3, "on_failure": "fail_fast"},
                    {"kind": "map", "target_node_id": "w2", "source_node_id": "tool", "source_path": "items", "on_failure": "fail_fast"},
                    {"kind": "tee", "target_node_ids": ["w3", "w4"], "on_failure": "fail_fast"}]},
                {"kind": "agent", "id": "worker", "agent_id": "operator"},
                {"kind": "agent", "id": "w2", "agent_id": "operator"},
                {"kind": "agent", "id": "w3", "agent_id": "operator"},
                {"kind": "agent", "id": "w4", "agent_id": "operator"},
                {"kind": "fan_in", "id": "merge", "aggregate_template": "{{ results }}"},
                {"kind": "end", "id": "end", "output_template": ""},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "decider"},
                {"kind": "conditional", "from_node": "decider", "router": {"kind": "json_path", "branches": [
                    {"conditions": [{"path": "done", "op": "eq", "value": True}], "to_node": "tool"}], "default_to": "sub"}},
                {"kind": "static", "from_node": "sub", "to_node": "tool"},
                {"kind": "static", "from_node": "tool", "to_node": "split"},
                {"kind": "static", "from_node": "worker", "to_node": "merge"},
                {"kind": "static", "from_node": "w2", "to_node": "merge"},
                {"kind": "static", "from_node": "w3", "to_node": "merge"},
                {"kind": "static", "from_node": "w4", "to_node": "merge"},
                {"kind": "static", "from_node": "merge", "to_node": "end"},
            ]})
        assert r.status_code == 201, r.text
    return parent, child


def _cleanup(base_url: str, *graph_ids: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for gid in graph_ids:
            c.delete(f"/v1/graphs/{gid}")


def _unnamed(page: Page, root: str) -> list[str]:
    return page.locator(root).locator("input, select, textarea").evaluate_all(_UNNAMED)


@pytest.mark.ui_e2e
def test_every_visible_control_of_every_kind_of_step_has_a_name(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    parent, child = _seed(base_url, unique_suffix)
    try:
        open_legacy_route(page, console_url, f"graphs/{parent}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(11, timeout=15_000)

        seen: dict[str, list[str]] = {}
        for i in range(rows.count()):
            rows.nth(i).click()
            expect(page.locator(gb.INSPECTOR)).to_be_visible(timeout=5_000)
            title = rows.nth(i).inner_text().replace("\n", " ")[:40]
            if "split" in title.lower():
                for split in _SPLITS:
                    page.locator(gb.INSPECTOR).get_by_text(split, exact=True).click()
                    seen[f"{title} / {split}"] = _unnamed(page, gb.INSPECTOR)
            else:
                seen[title] = _unnamed(page, gb.INSPECTOR)
        assert {k: v for k, v in seen.items() if v} == {}, "controls with no name, by step"

        page.locator(gb.OUTLINE_ADD).first.click()
        expect(page.locator(gb.PALETTE)).to_be_visible(timeout=5_000)
        assert _unnamed(page, gb.PALETTE) == []
    finally:
        _cleanup(base_url, parent, child)
