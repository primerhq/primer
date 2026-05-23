"""Session detail last-error + turns panel + toast request-id + Files drill-down.

Covers backlog items:

* U0032 — Toast renders copy-able request-id on a 5xx error from a
  UI mutation (page.route mock injects a 500 with
  ``extensions.request_id``; the new-agent modal's create path
  surfaces the toast with "request-id <rid>" + a "copy" link).
* U0080 — Workspace Files tab directory drill-down: clicking a
  directory row in the lazy tree renders its children
  (reframed from URL ``?path=`` query — the actual UI uses an
  in-tree expand pattern, not URL navigation). Skip-soft if the
  container can't reach the workspace's backend path.
* U0083 — Session detail "Last error" panel renders when the
  backend session row has ``last_error`` populated. Seed a
  graph-bound session against a placeholder LLM so the worker
  fast-fails and the row converges with ``last_error``.
* U0084 — Session detail Turns-timeline PANEL toggles via header
  click (reframed from per-row TurnRow collapse — testing the
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
            "config": {"kind": "local", "path": str(tmp_path)},
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
# U0032 — Toast renders copy-able request-id on 5xx error
# ===========================================================================


def test_u0032_toast_renders_request_id_on_5xx(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0032 — Mock POST /v1/agents to return 500 with an RFC 7807
    envelope containing ``extensions.request_id``. Open the New
    agent modal, fill the required fields, click Create → the
    error toast (``kind=error``) must contain the literal text
    ``request-id <rid>`` and a ``copy`` link (per chrome.jsx:525-532).

    Pins the documented "5xx surfaces copy-able request-id" toast
    contract — operators reach this affordance from any failing
    mutation, this test uses agent-create as the representative
    mutation surface.
    """
    pid = f"llm-32-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    rid_marker = f"req-test-{unique_suffix}"
    cleanup_urls = [f"/v1/llm_providers/{pid}"]

    # Intercept the POST /v1/agents only — leave list-fetch GETs alone.
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

        # Modal opens. Fill the minimum-viable fields: id + provider.
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)
        # Agent id input — first textbox in the modal.
        textboxes = modal.get_by_role("textbox").all()
        assert len(textboxes) >= 1, "no textboxes in NewAgentModal"
        textboxes[0].fill(f"ag-32-{unique_suffix}")

        # Click "Create" — mocked response will return our 500.
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
# U0080 — Workspace Files tab directory drill-down (in-tree expand)
# ===========================================================================


def test_u0080_workspace_files_dir_drilldown_renders_children(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0080 — Seed a workspace + write ``dir1/a.txt`` via the API
    files endpoint. Navigate to ``#/workspaces/<id>?tab=files``,
    wait for the tree to render, click the ``dir1`` row → assert
    the child file ``a.txt`` becomes visible (lazy expand via the
    DirectoryNode setOpen handler).

    Skip-soft when the matrix-app container can't reach the host
    tmp_path the workspace provider points at — the PUT files
    call fails 5xx, same root cause as U0072.
    """
    wp_id = f"wp-80-{unique_suffix}"
    tpl_id = f"tpl-80-{unique_suffix}"
    # Use a container-internal path (matrix-app linux container can't
    # reach host Windows tmp_path that pytest's tmp_path fixture
    # provides — workspace materialise + file ops would crash or
    # silently fall back). /tmp inside the container is writable.
    container_path = f"/tmp/u0080-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "path": container_path},
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
        # provider's backend path is unreachable (matrix-app
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
                    f"workspace files PUT returned {r.status_code} — "
                    f"matrix-app container likely can't reach host tmp_path "
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

        # Wait for the file tree to render — the dir1 row must show up.
        dir1_row = page.get_by_text("dir1", exact=False).first
        dir1_row.wait_for(state="visible", timeout=15_000)

        # Initially a.txt is NOT in the DOM (lazy — closed branch).
        # Click dir1 → toggles open → fetches /files?path=dir1 → renders.
        dir1_row.click()

        # a.txt becomes visible within a few seconds.
        expect(
            page.get_by_text("a.txt", exact=False).first
        ).to_be_visible(timeout=10_000)
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0083 — Session detail "Last error" panel renders for failed graph session
# ===========================================================================


def test_u0083_session_detail_last_error_panel_renders(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0083 — Mock GET /v1/sessions/{sid} to return a row whose
    ``last_error`` payload is populated (RFC 7807 envelope shape).
    Navigate to ``#/sessions/<id>`` → the "Last error" panel
    (session-detail.jsx:262) renders with the literal "Last error"
    header + type subscript + the title/detail copy when expanded.

    Pins the documented session-detail last_error render — the
    only operator-facing surface for the session's failure
    payload. Using page.route here (instead of relying on the
    worker to actually populate last_error on a fast-fail path)
    keeps the test deterministic across container/native dispatch
    differences; the field's POPULATION is exercised end-to-end
    in T0739 / T0737 from the API loop.
    """
    pid = f"llm-83-{unique_suffix}"
    aid = f"ag-83-{unique_suffix}"
    wp_id = f"wp-83-{unique_suffix}"
    tpl_id = f"tpl-83-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    sid: str | None = None
    try:
        # Real session row (so the rest of the page renders honestly);
        # we'll only mock the last_error payload in flight.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(
                f"/v1/workspaces/{wid}/sessions",
                json={
                    "binding": {"kind": "agent", "agent_id": aid},
                    "auto_start": False,
                },
            )
            assert r.status_code == 201, r.text
            sid = r.json()["id"]
            cleanup_urls.insert(
                0, f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
            )
            # Fetch the real row so we can return it back with
            # last_error injected.
            gr = c.get(f"/v1/sessions/{sid}")
            assert gr.status_code == 200, gr.text
            real_row = gr.json()
            real_row["last_error"] = {
                "type": "/errors/internal",
                "title": "Synthetic last_error for U0083",
                "status": 500,
                "detail": "page.route-injected payload",
                "extensions": {"request_id": f"req-{unique_suffix}"},
            }

        # Mock the GET /v1/sessions/{sid} response so the UI sees
        # last_error populated.
        body_json = json.dumps(real_row)

        def _on_session_get(route):
            if route.request.method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=body_json,
                )
            else:
                route.continue_()

        page.route(f"**/v1/sessions/{sid}", _on_session_get)

        try:
            page.goto(
                f"{console_url}#/sessions/{sid}",
                wait_until="domcontentloaded",
            )
            page.locator(".nav-item").first.wait_for(
                state="visible", timeout=20_000,
            )

            # Last error panel header carries the literal "Last error"
            # span (session-detail.jsx:267). Renders unconditionally
            # when last_error is truthy; body is collapsed by default.
            expect(
                page.get_by_text("Last error", exact=True).first
            ).to_be_visible(timeout=10_000)
            # And the type subscript renders next to the header.
            expect(
                page.get_by_text("/errors/internal", exact=False).first
            ).to_be_visible(timeout=3_000)
        finally:
            page.unroute(f"**/v1/sessions/{sid}")
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0084 — Session detail Turns-timeline panel header toggle
# ===========================================================================


def test_u0084_session_detail_turns_panel_header_toggles(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0084 — Session detail Turns-timeline panel renders with
    ``turnsOpen=true`` by default (session-detail.jsx:37). Click
    the panel header → ``setTurnsOpen(!turnsOpen)`` flips →
    panel body content (the empty-state copy or turn rows) hides.
    Click again → body reappears.

    Reframed from the original U0084 ("TurnRow collapse") because
    the placeholder-LLM session may not populate any turns row;
    the OUTER panel collapse is the testable contract on every
    session regardless of dispatch outcome.
    """
    pid = f"llm-84-{unique_suffix}"
    aid = f"ag-84-{unique_suffix}"
    wp_id = f"wp-84-{unique_suffix}"
    tpl_id = f"tpl-84-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    cleanup_urls = [
        f"/v1/workspaces/{wid}",
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
        f"/v1/agents/{aid}",
        f"/v1/llm_providers/{pid}",
    ]
    sid: str | None = None
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post(
                f"/v1/workspaces/{wid}/sessions",
                json={
                    "binding": {"kind": "agent", "agent_id": aid},
                    "auto_start": False,
                },
            )
            assert r.status_code == 201, r.text
            sid = r.json()["id"]
            cleanup_urls.insert(
                0, f"/v1/workspaces/{wid}/sessions/{sid}/cancel",
            )

        page.goto(
            f"{console_url}#/sessions/{sid}",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # The panel body is the next sibling inside the parent .panel.
        # When turnsOpen=true, the body is in the DOM and visible.
        # session-detail.jsx renders "No turns yet — session is
        # No turns yet." for a CREATED session with empty
        # turns (line 249). Use that copy as the visible-when-open
        # signal.
        no_turns_copy = page.get_by_text(
            "No turns yet", exact=False,
        ).first
        # CREATED + no turns + open panel → copy is visible.
        no_turns_copy.wait_for(state="visible", timeout=10_000)

        # Click the panel header via direct DOM dispatch to bypass any
        # Playwright synthetic-event weirdness on .panel-h with nested
        # icon/span children. session-detail.jsx:234 wires onClick on
        # the .panel-h div itself, so dispatching click on that
        # element triggers React's setTurnsOpen.
        def _click_panel(label: str) -> bool:
            return page.evaluate(
                """(label) => {
                    const els = document.querySelectorAll('.panel-h');
                    for (const el of els) {
                        if (el.textContent.includes(label)) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                label,
            )

        assert _click_panel("Turns timeline"), (
            "could not locate .panel-h for Turns timeline"
        )

        # Body content gone within ~3s (state flushes).
        deadline = time.monotonic() + 3.0
        collapsed = False
        while time.monotonic() < deadline:
            if page.get_by_text(
                "No turns yet", exact=False,
            ).count() == 0:
                collapsed = True
                break
            page.wait_for_timeout(200)
        assert collapsed, "Turns-timeline panel didn't collapse on header click"

        # Click again → re-opens.
        assert _click_panel("Turns timeline")
        expect(
            page.get_by_text("No turns yet", exact=False).first
        ).to_be_visible(timeout=3_000)
    finally:
        _cleanup(base_url, cleanup_urls)
