"""UI e2e: graph editor journey on the G6 canvas.

Default-skipped (tests/ui_e2e/conftest.py sets collect_ignore_glob for
test_*.py unless PRIMER_RUN_UI_E2E=1). NOT part of CI's default pytest run.

The editor renders through window.GR_Canvas (AntV G6, canvas-backed), so
there are no per-node DOM elements — the graph is pixels on a <canvas>.
This journey asserts the canvas mounts and that adding a step through the
purpose-first palette works end to end: the new step is created complete and
selected, so the inspector opens on it.

Migrated to the revamped builder (GB_Builder): the toolbar "Add node" ->
kind-dropdown gesture became "+ Add a step" -> a purpose palette, and the
id-keyed side panel became the label-keyed inspector.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from . import _graph_builder_helpers as gb


def _seed_graph(base_url: str, gid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": gid, "description": "g6 editor e2e",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "agent", "id": "drafter", "agent_id": "g6e2e-agent"},
                {"kind": "end", "id": "end", "output_template": ""},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "drafter"},
                {"kind": "static", "from_node": "drafter", "to_node": "end"},
            ],
        })
        assert r.status_code in (201, 409), r.text


def test_graph_editor_g6_journey(base_url, console_url, page) -> None:
    gid = "g6e2e-editor"
    _seed_graph(base_url, gid)

    page.goto(f"{console_url}#/graphs/{gid}")

    gb.wait_for_builder(page)

    # The G6 canvas mounts: the container plus a <canvas> the renderer drew on.
    canvas = page.locator(gb.CANVAS)
    expect(canvas).to_be_visible()
    expect(canvas.locator("canvas").first).to_be_visible()

    # Add a step through the purpose-first palette. The builder creates a
    # complete node and selects it, so the inspector opens on the new step.
    before = gb.outline_row_count(page)
    gb.add_finish_step(page)
    assert gb.outline_row_count(page) == before + 1
    expect(page.locator(gb.INSPECTOR)).to_be_visible()
    # The step name is the primary field and is editable inline, so the title
    # is an <input> - assert its value, not its (empty) text content.
    expect(page.locator('[data-testid="gb-inspector-title"]')).to_have_value("Finish")
