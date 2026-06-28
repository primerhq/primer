"""UI e2e: graph editor journey on the G6 canvas.

Default-skipped (tests/ui_e2e/conftest.py sets collect_ignore_glob for
test_*.py unless PRIMER_RUN_UI_E2E=1). NOT part of CI's default pytest run.

The editor renders through window.GR_Canvas (AntV G6, canvas-backed), so
there are no per-node DOM elements — the graph is pixels on a <canvas>.
This journey asserts the canvas mounts and that adding a node through the
toolbar palette works end to end: the new node auto-selects and the side
panel switches into its per-kind editor.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect


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

    # The G6 canvas mounts: the container plus a <canvas> the renderer drew on.
    canvas = page.locator('[data-testid="graph-canvas"]')
    expect(canvas).to_be_visible()
    expect(canvas.locator("canvas").first).to_be_visible()

    # Add an agent node via the toolbar palette. onAddNode auto-selects the new
    # node (id "agent_1"), so the side panel switches into the agent editor.
    page.get_by_role("button", name="Add node").click()
    page.locator('a.dd-item', has_text="Agent").first.click()
    expect(page.get_by_text("AGENT NODE", exact=False)).to_be_visible()
    expect(page.locator('input[value="agent_1"]').first).to_be_visible()
