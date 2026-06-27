"""UI e2e: graph run-view journey + health-issue journey.

Default-skipped (tests/ui_e2e/conftest.py sets collect_ignore_glob for
test_*.py unless PRIMER_RUN_UI_E2E=1). NOT part of CI's default pytest run.

1. Seed a begin -> agent -> end graph + a graph-bound session, open the
   session, see node status rings on the run-view canvas, click a node,
   see its inspector (status pill + Turn log section).
2. Seed a graph whose agent node references a missing Agent, open a
   session bound to it, assert the 'This graph cannot run' banner.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect


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

    page.goto(f"{console_url}#/sessions/{sid}")
    # Run view canvas renders a status ring per node (all pending pre-run).
    expect(page.locator('[data-testid="run-node-begin"]')).to_be_visible()
    expect(page.locator('[data-testid="run-node-drafter"]')).to_be_visible()
    expect(page.locator('[data-testid="run-node-end"]')).to_be_visible()
    # Click the drafter node -> inspector shows its id + a Turn log section.
    page.locator('[data-testid="run-node-drafter"]').click(force=True)
    expect(page.get_by_text("drafter", exact=False).first).to_be_visible()
    expect(page.get_by_text("Turn log", exact=False)).to_be_visible()


def test_graph_health_issue_journey(base_url, console_url, page, tmp_path) -> None:
    _seed_llm_provider(base_url, "rv-prov2")
    # Graph references an agent id that does NOT exist -> graph_status not ok.
    _seed_graph(base_url, "rv-graph-broken", "missing-agent-xyz")
    wid = _seed_workspace(base_url, "rv-wp2", "rv-tpl2", tmp_path)
    sid = _seed_graph_session(base_url, wid, "rv-graph-broken")

    page.goto(f"{console_url}#/sessions/{sid}")
    banner = page.locator('[data-testid="graph-cannot-run"]')
    expect(banner).to_be_visible()
    expect(banner.get_by_text("cannot run", exact=False)).to_be_visible()
