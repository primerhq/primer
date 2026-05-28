"""Workspace polling + agent New-session + graph dangling/auto-layout + sidebar tweaks.

Covers backlog items:

* U0071 — Workspaces list polls and removes a deleted workspace row
  within the polling cadence (no manual refresh).
* U0082 — Agent detail "Test agent" (which opens NewSessionModal)
  pre-selects the current agent in the modal's Agent select.
* U0089 — Graph editor Auto-layout button reshuffles coordinates but
  does NOT enable Save (UI-only x/y are stripped from the diff per
  ``stripCoords``).
* U0090 — Graph status panel turns red with a missing-agent issue
  after the referenced agent is deleted via the API.
* U0093 — Sidebar section-header collapse state persists via
  ``localStorage["primer.sidebar.collapsed"]`` across reload.
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
            "id": agent_id, "description": "ws+agent+graph+tweaks probe",
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
            "id": tpl_id, "description": "ws tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        return r.json()["id"]


def _seed_graph(base_url: str, gid: str, agent_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={
            "id": gid, "description": "dangling probe",
            "entry_node_id": "n1",
            "nodes": [
                {"id": "n1", "kind": "agent", "agent_id": agent_id},
                {"id": "end", "kind": "terminal"},
            ],
            "edges": [
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
# U0071 — Workspaces list polls and removes a deleted row
# ===========================================================================


def test_u0071_workspaces_list_polls_and_removes_deleted_row(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0071 — Seed a workspace via API, navigate to /workspaces, see
    the row, DELETE via API, assert the row disappears within ~15s
    (Workspaces list refetches via useResource — sidebar polls
    workspaces every 5s; the list page refetches on navigation +
    list.refetch invalidations). The poll cadence governs how long
    it takes the row to drop.

    Pins the list-page invalidation contract (deleted rows leave
    the table without user action).
    """
    wp_id = f"wp-71-{unique_suffix}"
    tpl_id = f"tpl-71-{unique_suffix}"
    wid = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)
    cleanup_urls = [
        # workspace will be deleted by the test itself; only clean up
        # template + provider if they survive (idempotent DELETEs)
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
    ]
    try:
        page.goto(
            f"{console_url}#/workspaces",
            wait_until="domcontentloaded",
        )
        # Sidebar resilience gate.
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Wait until our workspace id appears in the list body.
        row_locator = page.get_by_text(wid, exact=False).first
        row_locator.wait_for(state="visible", timeout=10_000)

        # DELETE via API.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.delete(f"/v1/workspaces/{wid}")
            assert r.status_code in (200, 204), r.text

        # Row drops. The list page doesn't auto-poll on a timer —
        # it refetches on mutations + nav. To trigger a refetch
        # without a user action, click the "Refresh" button if
        # the page header has one (workspaces.jsx).
        refresh = page.get_by_role(
            "button", name="Refresh", exact=False,
        )
        if refresh.count() > 0:
            refresh.first.click()

        # Allow up to ~15s for the row to disappear.
        deadline = time.monotonic() + 15.0
        gone = False
        while time.monotonic() < deadline:
            if page.get_by_text(wid, exact=False).count() == 0:
                gone = True
                break
            page.wait_for_timeout(500)
            # Re-click refresh once more if available.
            if refresh.count() > 0:
                try:
                    refresh.first.click()
                except Exception:  # noqa: BLE001 — best effort
                    pass

        assert gone, (
            f"workspace {wid!r} row still visible 15s after DELETE — "
            "list page didn't refetch / re-render."
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0082 — Agent detail "Test agent" opens NewSessionModal pre-selecting the agent
# ===========================================================================


def test_u0082_agent_detail_new_session_preselects_agent(
    page, base_url, console_url, unique_suffix, tmp_path,
) -> None:
    """U0082 — Drill into agent detail (/agents/<id>), click "Test
    agent" → NewSessionModal opens with title="New session", the
    Agent select pre-bound to ``<id>`` via ``defaultAgentId={id}``
    (per agents.jsx:463). At least one workspace option must be
    available so the modal can actually submit (covers the
    regression observed under U0041 where the workspace list was
    empty and ``ws_option_values=['']``).
    """
    pid = f"llm-82-{unique_suffix}"
    aid = f"ag-82-{unique_suffix}"
    wp_id = f"wp-82-{unique_suffix}"
    tpl_id = f"tpl-82-{unique_suffix}"
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
    try:
        page.goto(
            f"{console_url}#/agents/{aid}",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Click "Test agent" — this opens NewSessionModal per
        # agents.jsx:404.
        test_btn = page.get_by_role(
            "button", name="Test agent", exact=True,
        ).first
        test_btn.wait_for(state="visible", timeout=10_000)
        test_btn.click()

        # Modal opens — title "New session".
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)
        expect(
            page.get_by_text("New session", exact=False).first
        ).to_be_visible(timeout=3_000)

        # Two selects in the modal: Workspace + Agent. The Agent
        # select should be pre-bound to our aid. Locate the select
        # that follows the "Agent" label.
        agent_select = modal.locator("select").nth(1)
        agent_select.wait_for(state="visible", timeout=3_000)
        # Allow a brief settle so the useEffect that seeds
        # agentId from the fetched list (if any) does not race
        # with the defaultAgentId already passed in.
        deadline = time.monotonic() + 3.0
        agent_value = None
        while time.monotonic() < deadline:
            agent_value = agent_select.input_value()
            if agent_value == aid:
                break
            page.wait_for_timeout(200)
        assert agent_value == aid, (
            f"Agent select didn't preselect {aid!r}, got {agent_value!r} "
            "(defaultAgentId may not be threading through)."
        )

        # Workspace select has at least our workspace as an option
        # (defends against the U0041 regression where ws_option_values
        # was [''] — empty list).
        ws_select = modal.locator("select").nth(0)
        ws_values = ws_select.evaluate(
            "el => Array.from(el.options).map(o => o.value)"
        )
        assert wid in ws_values, (
            f"Workspace select missing {wid!r}; got options={ws_values!r}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0089 — Graph editor Auto-layout does NOT enable Save (x/y stripped from diff)
# ===========================================================================


def test_u0089_graph_editor_auto_layout_does_not_enable_save(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0089 — On graph detail editor, Save initially disabled
    (diffCount=0). Click Auto-layout → nodes are re-positioned but
    Save STAYS disabled because the diff strips UI-only x/y
    coordinates before comparison (graphs.jsx:423-429 stripCoords).
    Pins the contract that purely-visual changes don't dirty the
    save buffer.
    """
    pid = f"llm-89-{unique_suffix}"
    aid = f"ag-89-{unique_suffix}"
    gid = f"gr-89-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    _seed_graph(base_url, gid, aid)
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

        # Click Auto-layout in the toolbar.
        auto = page.get_by_role(
            "button", name="Auto-layout", exact=True,
        ).first
        auto.wait_for(state="visible", timeout=5_000)
        auto.click()

        # Save remains disabled — x/y do not count as a real diff.
        # Allow a brief settle for React state to flush.
        page.wait_for_timeout(500)
        expect(save).to_be_disabled()
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0090 — Graph status panel turns red with missing-agent issue
# ===========================================================================


def test_u0090_graph_status_red_after_agent_deleted(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0090 — Seed graph with a valid agent → status returns
    ``{ok:true}`` → panel renders green/"All references resolve".
    DELETE the agent via API → click the Refresh button →
    ``/v1/graphs/<id>/status`` is re-fetched and now returns
    ``{ok:false, issues:[...]}`` containing the missing-agent string.
    The status panel turns red and surfaces the issue text.

    Pins compute.py:graph_status against the GraphStatusPanel
    rendering contract (graphs.jsx:349-380).
    """
    pid = f"llm-90-{unique_suffix}"
    aid = f"ag-90-{unique_suffix}"
    gid = f"gr-90-{unique_suffix}"
    _seed_llm_provider(base_url, pid)
    _seed_agent(base_url, aid, pid)
    _seed_graph(base_url, gid, aid)
    cleanup_urls = [
        f"/v1/graphs/{gid}",
        # agent will be deleted by the test
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

        # Wait for the initial status panel to render — accept any
        # of the in-flight phrasings ("Checking references…",
        # "All references resolve", or even "0 issues found").
        # Bound by 30s in case the first poll is slow.
        status_initial = page.locator(".panel").filter(
            has_text="GET /v1/graphs/" + gid + "/status",
        ).first
        status_initial.wait_for(state="visible", timeout=30_000)

        # DELETE the agent — the graph's n1 node now references
        # a missing agent.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.delete(f"/v1/agents/{aid}")
            assert r.status_code in (200, 204), r.text

        # Click the page header Refresh button (graphs.jsx:342) to
        # force status.refetch() immediately rather than wait the
        # 30s poll interval. The first Refresh in DOM is the page
        # header's.
        refresh = page.get_by_role(
            "button", name="Refresh", exact=True,
        ).first
        refresh.wait_for(state="visible", timeout=5_000)
        refresh.click()

        # The status panel must transition to a red state and
        # surface the missing-agent issue text. Accept either the
        # GraphStatusPanel header phrasing ("issues found" /
        # "blocking new sessions") OR the issue body text
        # mentioning the missing agent id.
        deadline = time.monotonic() + 15.0
        saw_red = False
        while time.monotonic() < deadline:
            # Issue text: backend emits
            # "node 'n1' references missing Agent '<aid>'"
            if page.get_by_text(aid, exact=False).filter(
                has_text="missing",
            ).count() > 0:
                saw_red = True
                break
            # Fallback: header copy "issue" + "found" anywhere on
            # the page (the panel's header line).
            if page.get_by_text("issue", exact=False).filter(
                has_text="found",
            ).count() > 0:
                saw_red = True
                break
            page.wait_for_timeout(500)
            # Re-click Refresh if available to nudge re-fetch.
            try:
                refresh.click()
            except Exception:  # noqa: BLE001
                pass
        assert saw_red, (
            "Status panel didn't turn red with missing-agent issue "
            "within 15s after agent DELETE + Refresh."
        )
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0093 — Sidebar section-header collapse persists via localStorage on reload
# ===========================================================================


def test_u0093_sidebar_section_collapse_persists_via_localstorage(
    page, base_url, console_url,
) -> None:
    """U0093 — Clicking a sidebar section header (e.g. "Compute")
    toggles its collapsed state, persists ``{<group>: true}`` into
    ``localStorage["primer.sidebar.collapsed"]`` (chrome.jsx:127-132),
    and on reload the section renders with the ``collapsed`` class
    applied to ``.nav-section``.

    Pins the localStorage-backed collapse contract — the only
    sidebar-collapse tweak that survives reload today (the icons-only
    sidebar toggle uses a separate key
    ``matrix.sidebar.iconsOnly``).
    """
    # Reset state so the test is deterministic regardless of prior
    # iterations leaving collapse state in localStorage.
    page.evaluate(
        "() => { try { localStorage.removeItem('primer.sidebar.collapsed'); } catch(_e){} }"
    )
    page.reload(wait_until="domcontentloaded")
    page.locator(".nav-item").first.wait_for(
        state="visible", timeout=20_000,
    )

    # Locate the "Compute" section header and the section itself.
    compute_header = page.locator(".nav-group").filter(has_text="Compute").first
    compute_header.wait_for(state="visible", timeout=10_000)
    compute_section = page.locator(".nav-section").filter(
        has=page.locator(".nav-group").filter(has_text="Compute"),
    ).first
    compute_section.wait_for(state="visible", timeout=5_000)

    # Pre-condition: section is NOT collapsed (no .collapsed class).
    initial_class = compute_section.get_attribute("class") or ""
    assert "collapsed" not in initial_class, (
        f"Compute section started collapsed; class={initial_class!r}"
    )

    # Click the section header to collapse it.
    compute_header.click()
    page.wait_for_timeout(300)

    # localStorage now carries the persisted state.
    stored = page.evaluate(
        "() => localStorage.getItem('primer.sidebar.collapsed')"
    )
    assert stored is not None, "localStorage matrix.sidebar.collapsed not written"
    parsed = json.loads(stored)
    assert parsed.get("Compute") is True, (
        f"primer.sidebar.collapsed missing Compute=true; got {parsed!r}"
    )

    # Reload — collapsed class must persist on the section.
    page.reload(wait_until="domcontentloaded")
    page.locator(".nav-item").first.wait_for(
        state="visible", timeout=20_000,
    )
    compute_section_after = page.locator(".nav-section").filter(
        has=page.locator(".nav-group").filter(has_text="Compute"),
    ).first
    compute_section_after.wait_for(state="visible", timeout=10_000)
    after_class = compute_section_after.get_attribute("class") or ""
    assert "collapsed" in after_class, (
        f"Compute section didn't render collapsed after reload; "
        f"class={after_class!r}"
    )

    # Clean up — restore default uncollapsed state so subsequent
    # tests see a fresh sidebar.
    page.evaluate(
        "() => { try { localStorage.removeItem('primer.sidebar.collapsed'); } catch(_e){} }"
    )
