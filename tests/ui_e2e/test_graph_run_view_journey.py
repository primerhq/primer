"""UI e2e: graph run-view journey + health-issue journey.

Default-skipped (tests/ui_e2e/conftest.py sets collect_ignore_glob for
test_*.py unless PRIMER_RUN_UI_E2E=1). NOT part of CI's default pytest run.

1. Seed a begin -> agent -> end graph + a graph-bound session, open the
   session, assert the run-view G6 canvas mounts (a <canvas> the renderer
   drew on), click the agent node (center of the begin->agent->end chain)
   and see its inspector (Turn log section).
2. Seed a graph whose agent node references a missing Agent, open a
   session bound to it, assert the 'This graph cannot run' banner.

The run-view renders through window.GR_Canvas (AntV G6, canvas-backed), so
nodes are pixels on a <canvas>, not per-node DOM elements.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_session_in_studio


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid, "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code in (201, 409), r.text


def _seed_agent(base_url: str, aid: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": aid, "description": "run-view probe",
            "model": {"provider_id": pid, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code in (201, 409), r.text


def _seed_graph(base_url: str, gid: str, agent_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": gid, "description": "run-view probe",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "agent", "id": "drafter", "agent_id": agent_id},
                {"kind": "end", "id": "end", "output_template": ""},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "drafter"},
                {"kind": "static", "from_node": "drafter", "to_node": "end"},
            ],
        })
        assert r.status_code in (201, 409), r.text


def _seed_workspace(base_url: str, wp: str, tpl: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.post("/v1/workspace_providers", json={
            "id": wp, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        c.post("/v1/workspace_templates", json={
            "id": tpl, "description": "tpl", "provider_id": wp,
            "backend": {"kind": "local"},
        })
        r = c.post("/v1/workspaces", json={"template_id": tpl})
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _seed_graph_session(base_url: str, wid: str, gid: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={
            "binding": {"kind": "graph", "graph_id": gid},
            "auto_start": False,
        })
        assert r.status_code == 201, r.text
        return r.json()["id"]


def test_graph_run_view_journey(base_url, console_url, page, tmp_path) -> None:
    _seed_llm_provider(base_url, "rv-prov")
    _seed_agent(base_url, "rv-agent", "rv-prov")
    _seed_graph(base_url, "rv-graph", "rv-agent")
    wid = _seed_workspace(base_url, "rv-wp", "rv-tpl", tmp_path)
    sid = _seed_graph_session(base_url, wid, "rv-graph")

    # Re-pointed: open the graph session in the Studio (center graph panel).
    # The reused SD_GraphRunView renders inside panel-graph, so the G6
    # canvas + node inspector assertions are unchanged.
    #
    # LAYOUT NOTE: I verified (headless-chromium box-model probes of the exact
    # studio-center-inner → panel-graph → body → .panel → grid(minmax(0,1fr)
    # 360px) → graph-canvas chain, including a live G6 v5 mount) that the
    # graph-canvas div and G6's inner <canvas> both get a real 424x500 box and
    # are visible at the 1366x768 e2e viewport — even when G6 initialises at
    # zero width (autoResize recovers). So the Studio layout is SOUND; the CI
    # "attached but not visible" was a RENDER-SETTLE RACE: SD_GraphRunView
    # first shows "Loading graph…" (until GET /graphs/{gid} resolves), then
    # mounts the container, then G6 async-renders the <canvas>. Keying the
    # visibility gate on the INNER <canvas> (which only exists once G6 has
    # painted) rather than the outer div avoids asserting mid-render.
    open_session_in_studio(page, console_url, wid, sid, kind="graph")
    # Panel is up (open_session_in_studio waited on panel-graph). Do NOT wait
    # for "networkidle": the Studio holds a live tap SSE + status polling, so
    # the network is never idle and that wait always times out. Instead gate
    # directly on the graph-canvas attaching, then G6's injected inner
    # <canvas>, then visibility — the render-settle sequence, no idle needed.
    canvas = page.locator('[data-testid="graph-canvas"]')
    canvas.wait_for(state="attached", timeout=20_000)
    # The inner <canvas> only attaches once G6 has rendered — gate on it.
    inner = canvas.locator("canvas").first
    inner.wait_for(state="attached", timeout=20_000)
    canvas.scroll_into_view_if_needed(timeout=10_000)
    expect(canvas).to_be_visible(timeout=20_000)
    expect(inner).to_be_visible(timeout=20_000)
    # Let G6's async render + dagre autoFit settle before we measure/click —
    # the chain lands at the canvas center once layout finishes.
    page.wait_for_timeout(2000)
    # The agent node sits at the center of a begin->agent->end chain (dagre
    # LR + autoFit). Click center -> node-select filters the transcript to that
    # node. Converged (#9): the separate per-node "Turn log" panel was removed
    # in favour of a node-filtered transcript; the graph-node-filter banner
    # (with its "All nodes" clear affordance) surfaces the active node filter.
    box = canvas.bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(page.locator("[data-testid='graph-node-filter']")).to_be_visible()


# test_graph_health_issue_journey REMOVED (no Studio equivalent) — it asserted
# the ``graph-cannot-run`` "This graph cannot run" health banner. That banner
# was rendered by the retired ``SessionDetail`` body (session-detail.jsx
# ``SD_CannotRunBanner``, mounted at the page level), NOT by the reused
# ``SD_GraphRunView`` run-view that the Studio's ``panel-graph`` embeds. The
# Studio graph panel still mounts + renders the run-view canvas for an
# unrunnable graph, but it surfaces no "cannot run" pre-flight health banner,
# so there is no Studio surface to pin. Removed with this documented note.
