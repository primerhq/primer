"""Navigation surfaces + signal-button feedback flows.

Covers:
* U0036 — Toolset detail Config tab deep-link survives reload.
* U0043 — Topbar worker-pill click navigates to Workers page.
* U0046 — Sessions list filter input narrows rows by id substring.
* U0030 — Session cancel button transitions row to ended/cancelled.
"""

from __future__ import annotations

import time

import httpx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_llm_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": pid,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "ui-e2e probe",
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"


def _seed_workspace(
    base_url: str, wp_id: str, tpl_id: str, tmp_path,
) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed provider failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "ui-e2e template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        return r.json()["id"]


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# U0036 — Toolset Config tab deep-link survives reload
# ---------------------------------------------------------------------------


def test_u0036_toolset_config_tab_deep_link_survives_reload(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0036 — Sister of U0018 (agent Tools) / U0033 (agent Config) /
    U0034 (agent Metadata) and U0045 (toolset Tools) for the toolset
    Config tab. Navigate to ``#/toolsets/<id>?tab=config``, confirm
    Config is selected, reload, confirm URL + aria-selected="true"
    on Config survive.

    Priority 6 — routing. Pins the toolset-detail tab-routing
    contract from toolsets.jsx:324
    (``["config","tools","sessions"].includes(routerQuery.tab) ?
    routerQuery.tab : "config"``). Config is the default — defends
    against a reload regression where the default-fallback path
    strips the query string.

    Uses an MCP-stdio toolset (placeholder command) for setup —
    no upstream call required, the tab routing happens before any
    /tools fetch is triggered.
    """
    toolset_id = f"ts-u0036-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/toolsets", json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {
                    # `command` is a list (argv[0]+args style) — first
                    # entry is the executable, remaining are args.
                    "command": ["echo", "placeholder"],
                    "args": [],
                    "env": {},
                },
            },
        })
        assert r.status_code == 201, f"seed toolset failed: {r.text}"

    try:
        page.goto(
            f"{console_url}#/toolsets/{toolset_id}?tab=config",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        config_tab = page.get_by_role("tab", name="Config").first
        config_tab.wait_for(state="visible", timeout=5_000)
        assert config_tab.get_attribute("aria-selected") == "true", (
            f"Config tab not selected on initial deep-link nav; "
            f"aria-selected={config_tab.get_attribute('aria-selected')!r}"
        )

        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        config_tab_after = page.get_by_role("tab", name="Config").first
        config_tab_after.wait_for(state="visible", timeout=5_000)
        assert config_tab_after.get_attribute("aria-selected") == "true", (
            f"Config tab lost selected state after reload; "
            f"aria-selected={config_tab_after.get_attribute('aria-selected')!r}"
        )
        assert "tab=config" in page.url, (
            f"reload dropped ?tab=config query: {page.url}"
        )
    finally:
        _cleanup(base_url, [f"/v1/toolsets/{toolset_id}"])


# U0043 (topbar worker-pill click navigates to /workers) pruned
# 2026-05-25 — narrow nav primitive whose surface is already exercised
# by U0073 (worker pill reflects drain signal — clicks through to the
# Workers page state) and U0099 (sidebar workers count matches API,
# rendered on the Workers page). The pill→/workers click handler is a
# 3-line affordance defended in passing by every test that lands on
# the Workers page. File kept so the history grep lands here.


# ---------------------------------------------------------------------------
# U0046 — Sessions list filter narrows rows
# ---------------------------------------------------------------------------


def test_u0046_sessions_list_filter_narrows_rows_by_id_substring(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0046 — Seed three sessions via API on one workspace, open
    /sessions, type a substring of ONE session's id into the filter
    input, assert only the matching row renders and the other two
    are absent from the DOM.

    Priority 6 — list-page filter UX. Sister of U0037 (agents
    list). The filter applies to id + agent + workspace per
    sessions-list.jsx:37 — and session ids are backend-allocated
    (the API silently ignores any user-supplied id, same contract
    as workspaces). So the discriminator has to live in a field
    the test controls — agent_id is the natural choice. The test
    seeds three agents with distinct discriminator tokens
    (alpha / beta / gamma), then binds one session per agent.
    """
    provider_id = f"llm-u0046-{unique_suffix}"
    wp_id = f"wp-u0046-{unique_suffix}"
    tpl_id = f"wt-u0046-{unique_suffix}"
    agent_alpha = f"ag-u0046-alpha-{unique_suffix}"
    agent_beta = f"ag-u0046-beta-{unique_suffix}"
    agent_gamma = f"ag-u0046-gamma-{unique_suffix}"
    workspace_id: str | None = None
    session_ids: list[str] = []
    _seed_llm_provider(base_url, provider_id)
    for aid in (agent_alpha, agent_beta, agent_gamma):
        _seed_agent(base_url, aid, provider_id)
    workspace_id = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for aid in (agent_alpha, agent_beta, agent_gamma):
            r = c.post(
                f"/v1/workspaces/{workspace_id}/sessions",
                json={
                    "binding": {"kind": "agent", "agent_id": aid},
                    "auto_start": False,
                },
            )
            assert r.status_code == 201, (
                f"seed session for {aid!r} failed: {r.text}"
            )
            session_ids.append(r.json()["id"])

    try:
        page.goto(f"{console_url}#/sessions", wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            "Sessions", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Wait for at least one row per seeded agent to render.
        for aid in (agent_alpha, agent_beta, agent_gamma):
            page.locator(f"tr:has-text('{aid}')").first.wait_for(
                state="visible", timeout=15_000,
            )

        # Filter input — placeholder copy is "Filter id, agent,
        # workspace…" per sessions-list.jsx:177.
        filter_input = page.get_by_placeholder(
            "Filter id, agent, workspace", exact=False,
        ).first
        filter_input.wait_for(state="visible", timeout=5_000)
        # Discriminator unique to the beta agent's id (carrying the
        # per-test suffix prevents accidentally matching unrelated
        # rows from earlier test runs).
        filter_input.fill(f"u0046-beta-{unique_suffix}")

        # The beta session row stays; alpha + gamma must be gone.
        page.locator(f"tr:has-text('{agent_beta}')").first.wait_for(
            state="visible", timeout=5_000,
        )
        assert page.locator(f"tr:has-text('{agent_alpha}')").count() == 0, (
            "alpha session row still rendered after beta-only filter"
        )
        assert page.locator(f"tr:has-text('{agent_gamma}')").count() == 0, (
            "gamma session row still rendered after beta-only filter"
        )

        # Defence: clearing the filter restores all three rows.
        filter_input.fill("")
        for aid in (agent_alpha, agent_beta, agent_gamma):
            page.locator(f"tr:has-text('{aid}')").first.wait_for(
                state="visible", timeout=5_000,
            )
    finally:
        cleanup = [f"/v1/sessions/{sid}" for sid in session_ids]
        if workspace_id:
            cleanup.append(f"/v1/workspaces/{workspace_id}")
        cleanup.extend([
            f"/v1/workspace_templates/{tpl_id}",
            f"/v1/workspace_providers/{wp_id}",
            f"/v1/agents/{agent_alpha}",
            f"/v1/agents/{agent_beta}",
            f"/v1/agents/{agent_gamma}",
            f"/v1/llm_providers/{provider_id}",
        ])
        _cleanup(base_url, cleanup)


# ---------------------------------------------------------------------------
# U0030 — Session cancel button transitions session to terminal
# ---------------------------------------------------------------------------


def test_u0030_session_cancel_button_transitions_row_to_terminal(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0030 — Seed agent + workspace + CREATED session via API
    (auto_start=False so no real LLM call attempted). Open session
    detail page, click Cancel, confirm in the dialog, assert:

    * the confirm dialog closes,
    * a "Cancel signal sent" toast appears,
    * the status text on the page transitions to a terminal value
      (ended / cancelled / failed) within a polling interval,
    * the Cancel button becomes disabled (per session-detail.jsx:336
      ``disabled={isTerminal || cancelMut.loading}``).

    Priority 1 — mutation feedback (destructive signal). The
    page polls /sessions/{id} every 2s while non-terminal
    (session-detail.jsx:22), so the status caption catches up to
    the API state without a manual refresh.
    """
    provider_id = f"llm-u0030-{unique_suffix}"
    agent_id = f"ag-u0030-{unique_suffix}"
    wp_id = f"wp-u0030-{unique_suffix}"
    tpl_id = f"wt-u0030-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    _seed_llm_provider(base_url, provider_id)
    _seed_agent(base_url, agent_id, provider_id)
    workspace_id = _seed_workspace(base_url, wp_id, tpl_id, tmp_path)

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            f"/v1/workspaces/{workspace_id}/sessions",
            json={
                "binding": {"kind": "agent", "agent_id": agent_id},
                "auto_start": False,
            },
        )
        assert r.status_code == 201, f"seed session failed: {r.text}"
        session_id = r.json()["id"]

    try:
        page.goto(
            f"{console_url}#/sessions/{session_id}",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(session_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Click the page-level "Cancel" button (red, kind=danger) —
        # scope to the signals area to avoid matching the modal's
        # "Cancel" button which doesn't appear until after the first
        # click.
        cancel_btn = page.get_by_role("button", name="Cancel", exact=True).first
        cancel_btn.wait_for(state="visible", timeout=5_000)
        cancel_btn.click()

        # Confirm modal appears with "Cancel session" button.
        confirm_btn = page.get_by_role(
            "button", name="Cancel session", exact=True,
        ).first
        confirm_btn.wait_for(state="visible", timeout=5_000)
        confirm_btn.click()

        # Toast appears.
        page.get_by_text("Cancel signal sent", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Status caption transitions to a terminal value within one
        # polling interval (2s) — budget 15s to absorb React batching
        # and the worker-pool reaction time.
        terminal_words = ("ended", "cancelled", "failed", "completed")
        deadline = time.monotonic() + 15.0
        terminal_seen = False
        while time.monotonic() < deadline:
            body_text = (page.locator("body").text_content() or "").lower()
            if any(w in body_text for w in terminal_words):
                terminal_seen = True
                break
            page.wait_for_timeout(500)
        assert terminal_seen, (
            f"session status never transitioned to a terminal value "
            f"within 15s after cancel"
        )

        # Defence: the Cancel button is now disabled (the page
        # re-renders with isTerminal=true).
        cancel_btn_after = page.get_by_role(
            "button", name="Cancel", exact=True,
        ).first
        # Wait briefly for re-render after the poll caught up.
        deadline = time.monotonic() + 5.0
        disabled = False
        while time.monotonic() < deadline:
            disabled = cancel_btn_after.is_disabled()
            if disabled:
                break
            page.wait_for_timeout(250)
        assert disabled, (
            "Cancel button did not become disabled after session "
            "transitioned to terminal"
        )
    finally:
        cleanup = []
        if session_id:
            cleanup.append(f"/v1/sessions/{session_id}")
        if workspace_id:
            cleanup.append(f"/v1/workspaces/{workspace_id}")
        cleanup.extend([
            f"/v1/workspace_templates/{tpl_id}",
            f"/v1/workspace_providers/{wp_id}",
            f"/v1/agents/{agent_id}",
            f"/v1/llm_providers/{provider_id}",
        ])
        _cleanup(base_url, cleanup)
