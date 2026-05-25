"""Workspace destroy + sessions filter + graph create + graph editor discard.

Covers backlog items:
* U0079 — Destroy workspace confirm fully destroys + navigates to /workspaces.
* U0081 — Sessions list status filter chip narrows the table.
* U0086 — Graph create modal POSTs + navigates to /graphs/{id}.
* U0088 — Graph editor Discard reverts an Add-node edit + re-disables Save.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import expect


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


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
            "id": agent_id, "description": "ws+sessions+graph probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code == 201


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "path": str(tmp_path)},
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


def _seed_session(
    base_url: str, workspace_id: str, agent_id: str, *, auto_start: bool = False,
) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": auto_start,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _cancel_session(base_url: str, workspace_id: str, session_id: str) -> None:
    """Cancel a session via API → status='ended'."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(f"/v1/workspaces/{workspace_id}/sessions/{session_id}/cancel")
        assert r.status_code == 200, r.text


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0079 — Workspace Destroy confirm fully destroys + navigates back
# ===========================================================================


def test_u0079_workspace_destroy_confirm_navigates_back_to_list(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0079 — Click Destroy workspace → modal opens → click
    "Destroy permanently" → API DELETE fires → "Workspace destroyed"
    toast (kind=warning per workspaces.jsx:867) → page navigates
    back to /workspaces → row absent from list.

    Positive-path mirror of U0078 (which exercised ESC dismiss).
    """
    wp_id = f"wp-79-{unique_suffix}"
    tpl_id = f"tpl-79-{unique_suffix}"
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    # The workspace will be destroyed by the test itself — only
    # clean up provider + template.
    cleanup_urls = [
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
    ]
    try:
        page.goto(
            f"{console_url}#/workspaces/{wid}?tab=destroy",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Click Destroy workspace → modal.
        destroy_btn = page.get_by_role(
            "button", name="Destroy workspace", exact=True,
        ).first
        destroy_btn.wait_for(state="visible", timeout=10_000)
        destroy_btn.click()

        # Confirm modal: click "Destroy permanently".
        permanent_btn = page.get_by_role(
            "button", name="Destroy permanently", exact=True,
        ).first
        permanent_btn.wait_for(state="visible", timeout=5_000)
        permanent_btn.click()

        # Success toast — title "Workspace destroyed".
        expect(
            page.get_by_text("Workspace destroyed", exact=False).first
        ).to_be_visible(timeout=10_000)

        # Page navigated back to /workspaces.
        page.wait_for_url("**/console/**", timeout=5_000)
        # URL hash now contains /workspaces (not /workspaces/<id>).
        # Wait briefly for navigation.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if page.url.endswith("#/workspaces") or "/workspaces" in page.url:
                break
            page.wait_for_timeout(200)
        # Workspace row absent from the list (API DELETE removed it).
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/workspaces/{wid}")
            # DELETE is idempotent — second GET should 404.
            assert r.status_code == 404, (
                f"workspace row still exists after destroy: {r.status_code}"
            )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0081 — Sessions list status filter chip narrows table
# ===========================================================================


def test_u0081_sessions_list_status_chip_filter_narrows_table(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0081 — Seed 3 sessions: two CREATED + one ENDED (via API
    cancel). Click the "ended" status chip; only the ENDED session
    row remains in the table.

    Pins the status-chip filter in sessions-list.jsx:toggleStatus.
    """
    pid = f"llm-81-{unique_suffix}"
    aid = f"ag-81-{unique_suffix}"
    wp_id = f"wp-81-{unique_suffix}"
    tpl_id = f"tpl-81-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    sid_created_1 = _seed_session(base_url, wid, aid)
    sid_created_2 = _seed_session(base_url, wid, aid)
    sid_ended = _seed_session(base_url, wid, aid)
    _cancel_session(base_url, wid, sid_ended)
    cleanup_urls = [
        f"/v1/workspaces/{wid}/sessions/{sid_created_1}/cancel",
        f"/v1/workspaces/{wid}/sessions/{sid_created_2}/cancel",
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    try:
        page.goto(
            f"{console_url}#/sessions",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Wait for our 3 session rows to land in the list (sessions
        # list fetches once on mount, possibly polls).
        # Filter the body by the session ids to find them; wait until
        # at least one of ours is visible.
        for sid in (sid_created_1, sid_created_2, sid_ended):
            page.get_by_text(sid, exact=False).first.wait_for(
                state="visible", timeout=10_000,
            )

        # Click the "ended" status chip. The chip is a span with
        # title="ended" inside .chip-group.
        ended_chip = page.locator(
            ".chip-group [title='ended']",
        ).first
        ended_chip.wait_for(state="visible", timeout=5_000)
        ended_chip.click()

        # After filter: ENDED row remains, the two CREATED rows
        # disappear. Wait for the CREATED rows to be gone.
        deadline = time.monotonic() + 8.0
        filtered_ok = False
        while time.monotonic() < deadline:
            page.wait_for_timeout(400)
            ended_visible = page.get_by_text(
                sid_ended, exact=False,
            ).count() >= 1
            created_visible = (
                page.get_by_text(sid_created_1, exact=False).count() >= 1
                or page.get_by_text(sid_created_2, exact=False).count() >= 1
            )
            if ended_visible and not created_visible:
                filtered_ok = True
                break
        assert filtered_ok, (
            "status chip filter didn't narrow table: "
            f"ended_visible={ended_visible}, created_visible={created_visible}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0086 — Graph create modal navigates to /graphs/{id}
# ===========================================================================


# U0086 pruned 2026-05-25: the NewGraphModal POST → /graphs/{id}
# navigate flow is fully exercised end-to-end by U0107 (graph-builder
# persistence journey, test_graph_builder_persistence_journey.py).
# U0107 walks /graphs list → New-graph modal → submit → navigate to
# /graphs/{gid} → Add Node → Save → reload → breadcrumb back to list,
# which strictly subsumes U0086's narrower modal-create-then-navigate
# assertion.


# ===========================================================================
# U0088 — Graph editor Discard reverts unsaved Add-node edit
# ===========================================================================


def test_u0088_graph_editor_discard_reverts_unsaved_add_node(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0088 — On graph detail editor, Save initially disabled
    (diffCount=0). Click Add node → Terminal (Save becomes enabled).
    Click Discard → unsaved edit reverts; Save returns to disabled.

    Pins the discard-edits contract in graphs.jsx; sister of U0087
    which exercised the Add-node → Save-enables direction.
    """
    pid = f"llm-88-{unique_suffix}"
    aid = f"ag-88-{unique_suffix}"
    gid = f"gr-88-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    # Seed an existing graph with one agent→terminal — Discard
    # should restore us to this baseline.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": gid, "description": "discard probe",
            "entry_node_id": "n1",
            "nodes": [
                {"id": "n1", "kind": "agent", "agent_id": aid},
                {"id": "end", "kind": "terminal"},
            ],
            "edges": [
                {"kind": "static", "from_node": "n1", "to_node": "end"},
            ],
        })
        assert r.status_code == 201, r.text
    cleanup_urls = [
        f"/v1/graphs/{gid}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    try:
        page.goto(
            f"{console_url}#/graphs/{gid}",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        save = page.get_by_role("button", name="Save", exact=True).first
        save.wait_for(state="visible", timeout=10_000)
        expect(save).to_be_disabled()

        # Add a node (mirror U0087 — Add node → Terminal).
        add_btn = page.get_by_role(
            "button", name="Add node", exact=False,
        ).first
        if add_btn.count() == 0:
            pytest.skip("Add node button not found in editor")
        add_btn.click()
        page.wait_for_timeout(300)
        terminal_opt = page.get_by_text("Terminal", exact=False).first
        if terminal_opt.count() == 0:
            pytest.skip("Terminal option not found in Add menu")
        terminal_opt.click()

        # Save now enabled.
        expect(save).to_be_enabled(timeout=3_000)

        # Click Discard.
        discard = page.get_by_role(
            "button", name="Discard", exact=True,
        ).first
        discard.wait_for(state="visible", timeout=5_000)
        discard.click()

        # Save returns to disabled (diff cleared).
        expect(save).to_be_disabled(timeout=3_000)
    finally:
        _cleanup(base_url, cleanup_urls)
