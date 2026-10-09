"""Journey: a render error below the graph builder shows a message with Undo and Discard, and the console stays up (review of #646, round 2).

The console had no error boundary, so one component throwing while it drew a graph unmounted the root and the operator's unsaved draft went with it. The builder is wrapped in one now; the draft
lives in ``GB_Builder`` above the boundary. In the real console (real React) one step of the builder is made to throw for a draft whose description is ``POISON``, a spec with that
description is Loaded through the Import spec modal, and: the message appears and the rest of the console is still on screen; Undo brings back the graph as it was; and so does Discard.
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

_POISON_THE_INSPECTOR = """() => {
    const real = window.GB_Inspector;
    window.GB_Inspector = function (p) {
        if (p.draft && p.draft.description === 'POISON') throw new Error('poison in the inspector');
        return real(p);
    };
}"""

_POISON_A_SELECTED_STEP = """() => {
    const real = window.GB_Inspector;
    window.GB_Inspector = function (p) {
        if (p.node) throw new Error('poison in a selected step');
        return real(p);
    };
}"""

_NODES = [{"kind": "begin", "id": "begin"}, {"kind": "end", "id": "end", "output_template": ""}]
_EDGES = [{"kind": "static", "from_node": "begin", "to_node": "end"}]


def _load_poison(page: Page) -> None:
    page.locator('[data-testid="gb-json-tab"]').click()
    modal = page.locator(".modal").first
    expect(modal).to_be_visible(timeout=10_000)
    modal.get_by_test_id("graph-import-spec").fill(json.dumps({"description": "POISON", "nodes": _NODES + [{"kind": "agent", "id": "extra", "agent_id": "x"}], "edges": _EDGES}))
    modal.get_by_role("button", name="Load into editor").click()


@pytest.mark.ui_e2e
def test_a_render_error_shows_a_message_and_undo_and_discard_bring_the_graph_back(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"render-err-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "render error probe", "nodes": _NODES, "edges": _EDGES})
        assert r.status_code == 201, r.text
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(2, timeout=15_000)
        page.evaluate(_POISON_THE_INSPECTOR)

        _load_poison(page)
        message = page.get_by_test_id("gb-render-error")
        expect(message).to_be_visible(timeout=10_000)
        expect(message).to_contain_text("poison in the inspector")
        expect(page.locator('[data-testid^="nv-overlay:"]')).to_be_visible()   # the console is still on screen

        page.get_by_test_id("gb-render-error-undo").click()
        expect(message).to_have_count(0, timeout=5_000)
        expect(rows).to_have_count(2, timeout=10_000)                          # the graph as it was before the Load

        _load_poison(page)
        expect(message).to_be_visible(timeout=10_000)                          # the boundary caught the second one too
        page.get_by_test_id("gb-render-error-discard").click()
        expect(message).to_have_count(0, timeout=5_000)
        expect(rows).to_have_count(2, timeout=10_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")


@pytest.mark.ui_e2e
def test_a_step_that_throws_when_it_is_selected_can_be_let_go_of_without_losing_the_draft(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    graph_id = f"render-err-sel-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "click-time error probe", "nodes": _NODES, "edges": _EDGES})
        assert r.status_code == 201, r.text
    try:
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(2, timeout=15_000)
        page.evaluate(_POISON_A_SELECTED_STEP)

        rows.first.click()
        message = page.get_by_test_id("gb-render-error")
        expect(message).to_be_visible(timeout=10_000)
        expect(message).to_contain_text("poison in a selected step")
        expect(page.locator('[data-testid^="nv-overlay:"]')).to_be_visible()

        page.get_by_test_id("gb-render-error-clear").click()
        expect(message).to_have_count(0, timeout=5_000)
        expect(rows).to_have_count(2, timeout=10_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")
