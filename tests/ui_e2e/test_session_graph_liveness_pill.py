"""UI test: a graph-bound session's References panel reports real run
liveness, not a hardcoded ``executor missing`` stub.

Regression: ``session-detail.jsx`` rendered a literal
``<span class="pill pill-failed">executor missing</span>`` for EVERY
graph-bound session, regardless of state. It is now derived from the
session status + the owning worker's liveness:

* ``created``  -> "awaiting worker"
* ``running``  -> "live" (owner worker active) / "stalled" (dead/missing)
* ``paused``   -> "paused"
* terminal     -> no pill

This test pins the deterministic, LLM-free path: a CREATED graph
session (seeded with ``auto_start=False``) must show an "awaiting
worker" liveness pill and must NOT contain the old "executor missing"
literal anywhere on the page.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._studio_helpers import open_session_in_studio

pytestmark = smk("SMK-UI-02")


def _seed(base_url: str, suffix: str, tmp_path):
    """Seed agent + workspace + graph + a CREATED graph session.

    Returns (session_id, cleanup_urls). No LLM provider is needed: the
    session stays CREATED (auto_start=False) so no node is ever
    dispatched and the agent's model is never resolved.
    """
    aid = f"ag-gl-{suffix}"
    wp_id = f"wp-gl-{suffix}"
    tpl_id = f"tpl-gl-{suffix}"
    gid = f"gr-gl-{suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": aid,
            "description": "ui-e2e liveness probe agent",
            "model": {"provider_id": "none", "model_name": "none"},
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "ui-e2e ws template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed wp template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        wid = r.json()["id"]

        r = c.post("/v1/graphs", json={
            "id": gid,
            "description": "ui-e2e liveness probe graph",
            "nodes": [
                {"kind": "begin", "id": "begin"},
                {"kind": "agent", "id": "n", "agent_id": aid},
                {"kind": "end", "id": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "n"},
                {"kind": "static", "from_node": "n", "to_node": "end"},
            ],
        })
        assert r.status_code == 201, f"seed graph failed: {r.text}"

        r = c.post(
            f"/v1/workspaces/{wid}/sessions",
            json={
                "binding": {"kind": "graph", "graph_id": gid},
                "auto_start": False,  # stay CREATED -> deterministic liveness
            },
        )
        assert r.status_code == 201, f"seed graph session failed: {r.text}"
        sid = r.json()["id"]

    cleanup = [
        f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/graphs/{gid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
    ]
    return wid, sid, cleanup


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001 - best-effort
                pass


def test_graph_session_liveness_pill_replaces_executor_missing_stub(
    page,
    console_url: str,
    base_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    wid, sid, cleanup = _seed(base_url, unique_suffix, tmp_path)
    try:
        # Re-pointed to the Studio graph panel. The old hardcoded
        # "executor missing" stub + the References-panel "awaiting worker"
        # liveness pill both lived in the retired SessionDetail. The Studio's
        # panel-graph header derives its state from the reused StatusPill;
        # a CREATED graph session reads "created" there. This pins the
        # surviving invariant: the "executor missing" stub is gone
        # everywhere, and the panel surfaces the real (created) status.
        open_session_in_studio(page, console_url, wid, sid, kind="graph")

        # The hardcoded stub must be gone everywhere in the Studio.
        expect(page.get_by_text("executor missing")).to_have_count(0)

        # The graph panel header shows the derived StatusPill; a CREATED
        # session reads "created" (StatusPill labels — shared.jsx).
        header = page.locator('[data-testid="panel-graph-header"]')
        expect(header.locator(".pill").filter(has_text="created").first).to_be_visible(
            timeout=10_000,
        )
    finally:
        _cleanup(base_url, cleanup)
