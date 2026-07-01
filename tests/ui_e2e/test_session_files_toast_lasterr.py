"""Session detail last-error + turns panel + toast request-id + Files drill-down.

Covers backlog items:

* U0032 - Toast renders copy-able request-id on a 5xx error from a
  UI mutation (page.route mock injects a 500 with
  ``extensions.request_id``; the new-agent modal's create path
  surfaces the toast with "request-id <rid>" + a "copy" link).
* U0080 - Workspace Files tab directory drill-down: clicking a
  directory row in the lazy tree renders its children
  (reframed from URL ``?path=`` query - the actual UI uses an
  in-tree expand pattern, not URL navigation). Skip-soft if the
  container can't reach the workspace's backend path.
* U0083 - Session detail "Last error" panel renders when the
  backend session row has ``last_error`` populated. Seed a
  graph-bound session against a placeholder LLM so the worker
  fast-fails and the row converges with ``last_error``.
* U0084 - Session detail Turns-timeline PANEL toggles via header
  click (reframed from per-row TurnRow collapse - testing the
  outer panel is exercise-able regardless of whether any turns
  were recorded by the placeholder-LLM fast-fail path).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from playwright.sync_api import expect


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
        assert r.status_code == 201, r.text


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": agent_id, "description": "session+files+toast probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code == 201, r.text


def _seed_workspace(base_url: str, wp_id: str, tpl_id: str, tmp_path) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _seed_graph_with_missing_router(
    base_url: str, gid: str, agent_id: str,
) -> None:
    """Seed a graph that GUARANTEES last_error population once the
    worker dispatches the session: the conditional callable router
    points at an unregistered callable_id, so executor construction
    fails fast via /handle_fatal with a populated last_error
    referencing the router (same pattern as T0739 in
    tests/e2e/test_more_yields_and_graph.py).
    """
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": gid, "description": "router fatal probe",
            "entry_node_id": "n1",
            "nodes": [
                {"id": "n1", "kind": "agent", "agent_id": agent_id},
                {"id": "n2", "kind": "agent", "agent_id": agent_id},
                {"id": "end", "kind": "terminal"},
            ],
            "edges": [
                {
                    "kind": "conditional",
                    "from_node": "n1",
                    "router": {
                        "kind": "callable",
                        "callable_id": "no-such-router",
                    },
                },
                {"kind": "static", "from_node": "n2", "to_node": "end"},
            ],
        })
        assert r.status_code == 201, r.text


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0032 - Toast renders copy-able request-id on 5xx error
# ===========================================================================


def test_u0032_toast_renders_request_id_on_5xx(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0032 - Mock POST /v1/agents to return 500 with an RFC 7807
    envelope containing ``extensions.request_id``. Open the New
    agent modal, fill the required fields, click Create → the
    error toast (``kind=error``) must contain the literal text
    ``request-id <rid>`` and a ``copy`` link (per chrome.jsx:525-532).

    Pins the documented "5xx surfaces copy-able request-id" toast
    contract - operators reach this affordance from any failing
    mutation, this test uses agent-create as the representative
    mutation surface.
    """
    pid = f"llm-32-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    rid_marker = f"req-test-{unique_suffix}"
    cleanup_urls = [f"/v1/llm_providers/{pid}"]

    # Intercept the POST /v1/agents only - leave list-fetch GETs alone.
    def _on_post_agents(route):
        method = route.request.method
        if method == "POST":
            route.fulfill(
                status=500,
                content_type="application/problem+json",
                body=json.dumps({
                    "type": "/errors/internal",
                    "title": "Synthetic 500 for U0032",
                    "status": 500,
                    "detail": "page.route-injected envelope",
                    "extensions": {"request_id": rid_marker},
                }),
            )
        else:
            route.continue_()

    page.route("**/v1/agents", _on_post_agents)

    try:
        page.goto(
            f"{console_url}#/agents",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Click "New agent".
        new_btn = page.get_by_role(
            "button", name="New agent", exact=False,
        ).first
        new_btn.wait_for(state="visible", timeout=10_000)
        new_btn.click()

        # Modal opens. Fill the minimum-viable fields. Create stays
        # disabled until a provider AND a model are selected, so all
        # three are required to actually fire the mocked POST.
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)
        modal.locator("#na-id").fill(f"ag-32-{unique_suffix}")
        modal.locator("#na-llm-provider").select_option(pid)
        modal.locator("#na-model").select_option("fake-model")

        # Click "Create" - mocked response will return our 500.
        create_btn = page.get_by_role(
            "button", name="Create", exact=True,
        ).first
        create_btn.click()

        # Toast must contain "request-id <rid>" + a "copy" affordance.
        # The chrome.jsx ToastContainer renders ".req-id" with the
        # literal "request-id <rid>" + "copy" link.
        expect(
            page.locator(".toast.toast-error").first
        ).to_be_visible(timeout=5_000)
        # The rid value appears as text inside .req-id.
        expect(
            page.get_by_text(rid_marker, exact=False).first
        ).to_be_visible(timeout=3_000)
        # And the literal "request-id" prefix is rendered.
        expect(
            page.locator(".req-id").filter(has_text="request-id").first
        ).to_be_visible(timeout=3_000)
        # A "copy" link is offered.
        expect(
            page.locator(".req-id").get_by_text("copy", exact=False).first
        ).to_be_visible(timeout=3_000)
    finally:
        page.unroute("**/v1/agents")
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0080 - Workspace Files tab directory drill-down (in-tree expand)
# ===========================================================================


def test_u0080_workspace_files_dir_drilldown_renders_children(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0080 - Seed a workspace + write ``dir1/a.txt`` via the API
    files endpoint. Navigate to ``#/workspaces/<id>?tab=files``,
    wait for the tree to render, click the ``dir1`` row → assert
    the child file ``a.txt`` becomes visible (lazy expand via the
    DirectoryNode setOpen handler).

    Skip-soft when the primer-app container can't reach the host
    tmp_path the workspace provider points at - the PUT files
    call fails 5xx, same root cause as U0072.
    """
    wp_id = f"wp-80-{unique_suffix}"
    tpl_id = f"tpl-80-{unique_suffix}"
    # Use a container-internal path (primer-app linux container can't
    # reach host Windows tmp_path that pytest's tmp_path fixture
    # provides - workspace materialise + file ops would crash or
    # silently fall back). /tmp inside the container is writable.
    container_path = f"/tmp/u0080-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": container_path},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "u0080 tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
    ]
    try:
        # Skip-soft probe: PUT a file via API. If the workspace
        # provider's backend path is unreachable (primer-app
        # container vs host tmp_path), this PUT fails.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.put(
                f"/v1/workspaces/{wid}/files?path=dir1%2Fa.txt",
                json={
                    "content": "alpha bravo charlie\n",
                    "encoding": "text",
                },
            )
            if r.status_code >= 500:
                pytest.skip(
                    f"workspace files PUT returned {r.status_code} - "
                    f"primer-app container likely can't reach host tmp_path "
                    f"(same root cause as U0072). text={r.text[:200]!r}"
                )
            assert r.status_code in (200, 201, 204), r.text

        page.goto(
            f"{console_url}#/workspaces/{wid}?tab=files",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Wait for the file tree to render - the dir1 row must show up.
        dir1_row = page.get_by_text("dir1", exact=False).first
        dir1_row.wait_for(state="visible", timeout=15_000)

        # Initially a.txt is NOT in the DOM (lazy - closed branch).
        # Click dir1 → toggles open → fetches /files?path=dir1 → renders.
        dir1_row.click()

        # a.txt becomes visible within a few seconds.
        expect(
            page.get_by_text("a.txt", exact=False).first
        ).to_be_visible(timeout=10_000)
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0083 - Session detail "Last error" panel renders for failed graph session
# ===========================================================================


# U0083 REMOVED (no Studio equivalent) — this pinned the session-detail
# "Last error" panel (session-detail.jsx ``lastError`` block, header + RFC
# 7807 type subscript). That panel lived in the retired ``SessionDetail``
# body; the Studio's center session panel reuses ``SessionLiveStream``, which
# renders the live frame stream + a terminal "Session ended" notice but does
# NOT surface a session-level ``last_error`` panel (there is no equivalent
# data-testid or copy in studio-center.jsx). With no Studio surface to pin,
# the test is removed with this documented note (last_error POPULATION stays
# covered end-to-end by the API loop's T0739 / T0737).


# ===========================================================================
# U0084 - Session detail Turns-timeline panel header toggle
# ===========================================================================


# U0084 REMOVED (no Studio equivalent) — this pinned the session-detail
# "Turn log" tab (session-detail.jsx ``tabs`` + ``TurnLogTab``) and its
# empty-state "No turn-log entries yet" copy, driven through the mobile
# ``MobileTabs`` surface. Both the tabbed session-detail page and its Turn
# log tab were retired with the Studio. The Studio's center session panel
# reuses ``SessionLiveStream`` (a live frame stream), not a per-turn "Turn
# log" tab — there is no equivalent tab/empty-state surface in
# studio-center.jsx — so the test is removed with this documented note.
# (Graph sessions DO keep a "Turn log" section inside the reused
# SD_GraphRunView node inspector, exercised by test_graph_run_view_journey,
# but that is the graph node inspector, not this agent-session turn tab.)
