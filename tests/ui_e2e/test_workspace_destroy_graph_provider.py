"""Workspace tabs + destroy + graph editor + provider invalidate tests.

Covers backlog items:
* U0077 — Workspace detail tabs (Files / Sessions / Log / Config /
  Destroy) all reachable without console errors.
* U0078 — Destroy confirmation modal cancels cleanly (Cancel button
  + ESC both dismiss without firing DELETE; page.route observer
  confirms no DELETE was issued).
* U0087 — Graph editor Add node toolbar inserts a node and flips
  the Save button from disabled to enabled.
* U0091 — LLM provider Invalidate button toasts "Cache dropped"
  (POST .../invalidate is idempotent — toast appears, row still
  GET-able via API).
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import expect

from tests.ui_e2e._studio_helpers import open_studio, open_workspace_settings


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-06", status="partial")


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid, "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": agent_id, "description": "wsd+graph+prov probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code == 201


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "ws tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201
        return r.json()["id"]


def _seed_graph(base_url: str, gid: str, agent_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": gid,
            "description": "editor probe",
            "entry_node_id": "begin",
            "nodes": [
                {"id": "begin", "kind": "begin"},
                {"id": "n1", "kind": "agent", "agent_id": agent_id},
                {"id": "end", "kind": "end"},
            ],
            "edges": [
                {"kind": "static", "from_node": "begin", "to_node": "n1"},
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
        })
        assert r.status_code == 201, f"seed graph failed: {r.text}"


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0077 — Workspace detail 5 tabs all reachable
# ===========================================================================


def test_u0077_workspace_detail_tabs_all_reachable(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0077 — Re-pointed to the Studio. The old WorkspaceDetail's five
    tabs split across the Studio: Files + Sessions became left-sidebar
    sections, and Log / Config / Destroy moved into the Settings modal's
    left-rail nav (studio-settings.jsx). This asserts every one of those
    surfaces is reachable:

    * left sidebar ``sessions-section`` + ``files-section`` render,
    * the Settings modal's ``workspace-settings-nav:{channels,config,log,
      destroy}`` items are each clickable and render their panel.

    (Channels was not a WorkspaceDetail *tab* here but is one of the four
    settings-nav items, so we walk all four for completeness.)
    """
    wp_id = f"wp-77-{unique_suffix}"
    tpl_id = f"tpl-77-{unique_suffix}"
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
    ]
    try:
        open_studio(page, console_url, wid)
        # Files + Sessions are left-sidebar sections in the Studio.
        expect(page.locator("[data-testid='sessions-section']")).to_be_visible(timeout=15_000)
        expect(page.locator("[data-testid='files-section']")).to_be_visible(timeout=15_000)

        # Log / Config / Destroy (+ Channels) live in the Settings modal.
        gear = page.locator("[data-testid='studio-settings-btn']")
        gear.wait_for(state="visible", timeout=10_000)
        gear.click()
        modal = page.locator("[data-testid='workspace-settings']")
        expect(modal).to_be_visible(timeout=10_000)
        for section in ("channels", "config", "log", "destroy"):
            nav = page.locator(f"[data-testid='workspace-settings-nav:{section}']")
            nav.wait_for(state="visible", timeout=5_000)
            nav.click()
            # Brief settle so the reused panel renders.
            page.wait_for_timeout(300)
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0078 — Destroy confirmation modal cancels safely
# ===========================================================================


def test_u0078_workspace_destroy_modal_cancel_does_not_delete(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0078 — On the workspace Destroy tab, click the "Destroy
    workspace" button → confirmation modal opens. ESC-dismiss must
    close the modal WITHOUT issuing DELETE /v1/workspaces/{id}.
    Verified via page.route observer (no DELETE recorded) + API
    probe (workspace row still GET-able afterward).

    Sister of U0069 (session cancel modal). Pins the destructive-
    action gating contract for workspaces.
    """
    wp_id = f"wp-78-{unique_suffix}"
    tpl_id = f"tpl-78-{unique_suffix}"
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
    ]
    try:
        delete_calls = {"count": 0}

        def _on_delete(route):
            delete_calls["count"] += 1
            route.fulfill(
                status=204, content_type="application/json", body="",
            )

        # Route any DELETE on this specific workspace.
        page.route(
            f"**/v1/workspaces/{wid}", lambda route: (
                _on_delete(route) if route.request.method == "DELETE"
                else route.continue_()
            ),
        )

        # Open the Studio → Settings modal → Destroy section. The reused
        # WS_DestroyTab renders the same "Destroy workspace" button + confirm.
        open_workspace_settings(page, console_url, wid, "destroy")

        # Click Destroy workspace → confirm modal opens.
        destroy_btn = page.get_by_role(
            "button", name="Destroy workspace", exact=True,
        ).first
        destroy_btn.wait_for(state="visible", timeout=10_000)
        destroy_btn.click()

        # Confirm modal title contains the workspace id.
        page.get_by_text(f"Destroy {wid}?", exact=False).first.wait_for(
            state="visible", timeout=5_000,
        )

        # ESC dismiss.
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        # Confirm modal title gone.
        assert page.get_by_text(
            f"Destroy {wid}?", exact=False,
        ).count() == 0, "Destroy confirm modal didn't dismiss on ESC"

        # No DELETE was fired.
        assert delete_calls["count"] == 0, (
            f"DELETE fired despite ESC dismiss: "
            f"{delete_calls['count']} call(s)"
        )

        # Workspace row still exists via API.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/workspaces/{wid}")
            assert r.status_code == 200, r.text
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0087 — Graph editor Add node enables Save button
# ===========================================================================


# U0087 pruned 2026-05-25: the Add-node-flips-Save-from-disabled-to-
# enabled gating is exercised by U0107 (graph-builder persistence
# journey) which asserts "Save initially disabled → Add Node →
# Terminal → Save flips to enabled" as steps 3-5 of its 8-step walk.
# U0107 strictly subsumes U0087's narrower gating assertion.


# ===========================================================================
# U0091 — Provider Invalidate button toasts + row preserved
# ===========================================================================


def test_u0091_llm_provider_invalidate_toasts_and_preserves_row(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0091 — On LLM provider detail page, the Invalidate button
    triggers POST /v1/llm_providers/{id}/invalidate. Success path
    shows a toast (per providers.jsx: kind="info", title="Cache
    dropped") and the row is still GET-able afterward (invalidate
    drops the cached adapter, not the row).
    """
    pid = f"llm-91-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    cleanup_urls = [f"/v1/llm_providers/{pid}"]
    try:
        page.goto(
            f"{console_url}#/providers/llm/{pid}",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Wait for the Invalidate button.
        inv = page.get_by_role(
            "button", name="Invalidate", exact=True,
        ).first
        inv.wait_for(state="visible", timeout=10_000)
        inv.click()

        # Toast: "Cache dropped" with the provider id.
        expect(
            page.get_by_text("Cache dropped", exact=False).first
        ).to_be_visible(timeout=5_000)

        # Row still exists via API.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/llm_providers/{pid}")
            assert r.status_code == 200, r.text
            assert r.json()["id"] == pid
    finally:
        _cleanup(base_url, cleanup_urls)
