"""Anomaly-surface regression tests.

Each documented backend anomaly that the UI is supposed to surface has
its own test here. Setup creates the backend precondition via API
(httpx), then asserts the UI renders the documented surface.

Covers:
* U0008 — T0711 anomaly banner on toolset detail Tools tab when an
  MCP-HTTP toolset points at an unreachable URL (server returns 500).

UI spec §5 documents this surface as required.
"""

from __future__ import annotations

import time

import httpx


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-02", "SMK-UI-05", status="partial")


def test_u0008_toolset_tools_tab_renders_t0711_anomaly_banner(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0008 — Create an MCP-HTTP toolset via API pointing at an
    unreachable URL. Opening its detail page's Tools tab must render
    the documented T0711 anomaly banner (not a generic Error nor a
    blank-page crash).

    Priority 3 — anomaly surface. The Tools tab calls
    GET /v1/toolsets/{id}/tools which leaks 500 /errors/internal
    when the MCP-HTTP transport's upstream is unreachable. The UI
    detects (tools.error.status === 500 && config.transport === "http")
    and renders a dedicated Banner with retry + invalidate actions.
    """
    toolset_id = f"ts-u0008-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Create MCP-HTTP toolset pointing at a deliberately
        # unreachable URL — port 9999 on localhost is unlikely to
        # have anything listening.
        r = c.post("/v1/toolsets", json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "http",
                "config": {
                    "url": "http://127.0.0.1:9999/sse",
                    "headers": {},
                },
            },
        })
        assert r.status_code == 201, f"seed toolset failed: {r.text}"

    try:
        # Navigate to the toolset detail page; default tab loads, then
        # click Tools tab to trigger the /tools fetch.
        page.goto(
            f"{console_url}#/toolsets/{toolset_id}",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(toolset_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Click the Tools tab. The detail page has Config / Tools /
        # Metadata tabs; "Tools" is the role-name target.
        page.get_by_role("tab", name="Tools").first.click()

        # The anomaly banner has title "Tools list unavailable" and
        # detail mentioning T0711. Wait for the banner — the fetch
        # has to actually hit the backend + 500 first.
        page.get_by_text("Tools list unavailable", exact=False).first.wait_for(
            state="visible", timeout=15_000,
        )

        # Defence: the documented T0711 reference must appear in the
        # banner detail (so a copy-edit that drops it gets caught).
        page_text = page.locator("body").inner_text()
        assert "T0711" in page_text, (
            "T0711 reference missing from the rendered anomaly banner — "
            f"copy drift?\n(body text — truncated for readability):\n"
            f"{page_text[:1500]}"
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/toolsets/{toolset_id}")
            except Exception:  # noqa: BLE001
                pass


def test_u0018_deep_link_reload_preserves_agent_detail_tools_tab(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0018 — Reloading the browser on an agent detail deep-link
    with ``?tab=tools`` re-renders the same tab selected, not the
    default Config tab.

    Priority 6 — routing. The tab is read from routerQuery.tab and
    validated against AGENT_TABS (agents.jsx:363). Reload preserves
    the URL hash + query so the tab choice survives.
    """
    # Seed an LLM provider + agent so the detail page has data.
    provider_id = f"llm-u0018-{unique_suffix}"
    agent_id = f"ag-u0018-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "u0018 deep-link probe",
            "model": {"provider_id": provider_id, "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

    try:
        # Navigate directly to the deep-link with ?tab=tools.
        deep_link = f"{console_url}#/agents/{agent_id}?tab=tools"
        page.goto(deep_link, wait_until="domcontentloaded")

        # Wait for the agent detail page to render.
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Helper: the active tab is rendered with class "active" or
        # similar; using role+name + aria-selected is robust.
        tools_tab = page.get_by_role("tab", name="Tools").first
        tools_tab.wait_for(state="visible", timeout=5_000)
        # Tab should already be selected before reload — sanity check.
        # If not selected, the tab routing is broken (Config would be
        # default).
        assert tools_tab.get_attribute("aria-selected") == "true" \
            or "active" in (tools_tab.get_attribute("class") or ""), (
            f"Tools tab not selected after initial deep-link nav; "
            f"aria-selected={tools_tab.get_attribute('aria-selected')!r}, "
            f"class={tools_tab.get_attribute('class')!r}"
        )

        # Reload — the URL + query should survive.
        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # After reload, Tools tab MUST still be selected.
        tools_tab_after = page.get_by_role("tab", name="Tools").first
        tools_tab_after.wait_for(state="visible", timeout=5_000)
        assert (
            tools_tab_after.get_attribute("aria-selected") == "true"
            or "active" in (tools_tab_after.get_attribute("class") or "")
        ), (
            "Tools tab lost its selected state after reload — "
            "deep-link ?tab= query not preserved. "
            f"aria-selected={tools_tab_after.get_attribute('aria-selected')!r}, "
            f"class={tools_tab_after.get_attribute('class')!r}"
        )

        # Defence: URL still has ?tab=tools after reload.
        assert "tab=tools" in page.url, (
            f"reload dropped the ?tab=tools query: {page.url}"
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/agents/{agent_id}")
            except Exception:  # noqa: BLE001
                pass
            try:
                c.delete(f"/v1/llm_providers/{provider_id}")
            except Exception:  # noqa: BLE001
                pass


def test_u0013_session_detail_renders_t0399_stale_cache_notice(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    tmp_path,
) -> None:
    """U0013 — Opening any session detail page renders the documented
    T0399 / T0555 / T0611 stale-cache notice block ("workspace path
    is known to drift after signals") under the live status panel.

    Priority 3 — anomaly surface. The notice is unconditional per
    design §3.7 (session-detail.jsx:413-422): every session detail
    view must surface it so operators interpret nested workspace
    paths as informational, not authoritative. This test seeds the
    minimal precondition (agent + workspace + CREATED session)
    through the API and asserts the banner copy + the three documented
    references are present.

    Setup ladder mirrors test_t0042 (test_sessions_top_level.py:42-):
    LLM provider → agent → workspace provider → workspace template →
    workspace → session bound to the agent with auto_start=False so
    the worker pool doesn't attempt a real LLM call.
    """
    provider_id = f"llm-u0013-{unique_suffix}"
    agent_id = f"ag-u0013-{unique_suffix}"
    wp_id = f"wp-u0013-{unique_suffix}"
    tpl_id = f"wt-u0013-{unique_suffix}"
    workspace_id: str | None = None
    session_id: str | None = None
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # 1. LLM provider — placeholder (no upstream call).
        r = c.post("/v1/llm_providers", json={
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"

        # 2. Agent bound to the LLM provider.
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "u0013 session-detail probe",
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
            "tools": [],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

        # 3. Workspace provider + template + workspace.
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": str(tmp_path)},
        })
        assert r.status_code == 201, f"seed wp failed: {r.text}"
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id,
            "description": "u0013 template",
            "provider_id": wp_id,
            "backend": {"kind": "local"},
        })
        assert r.status_code == 201, f"seed template failed: {r.text}"
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, f"seed workspace failed: {r.text}"
        workspace_id = r.json()["id"]

        # 4. Session bound to the agent, auto_start=False so the
        # worker pool doesn't try a real LLM call (placeholder key).
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
        # The detail page renders the session id in its title.
        page.locator("h1.page-title").get_by_text(session_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # The stale-cache banner copy ("Reads are authoritative") is the
        # documented title; the detail line carries the three test
        # references. Asserting both ensures we don't accidentally match
        # an unrelated banner.
        page.get_by_text("Reads are authoritative", exact=False).first.wait_for(
            state="visible", timeout=5_000,
        )
        page.get_by_text("T0399", exact=False).first.wait_for(
            state="visible", timeout=5_000,
        )

        # Defence: all three references appear together in the same
        # detail line — pins the exact contract from §3.7. Use one
        # locator to keep the assertion contained.
        notice = page.get_by_text(
            "T0399 / T0555 / T0611", exact=False,
        ).first
        notice.wait_for(state="visible", timeout=5_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for url in (
                f"/v1/sessions/{session_id}" if session_id else None,
                f"/v1/workspaces/{workspace_id}" if workspace_id else None,
                f"/v1/workspace_templates/{tpl_id}",
                f"/v1/workspace_providers/{wp_id}",
                f"/v1/agents/{agent_id}",
                f"/v1/llm_providers/{provider_id}",
            ):
                if url is None:
                    continue
                try:
                    c.delete(url)
                except Exception:  # noqa: BLE001
                    pass


def test_u0009_agent_tools_tab_isolates_one_failing_toolset(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0009 — An agent bound to TWO toolsets — one that loads
    cleanly and one whose ``/tools`` endpoint 500-leaks — renders the
    good toolset's tools and the bad toolset's T0711 banner side by
    side. The page must NOT blank out; the failure must be confined
    to the offending toolset's panel.

    Priority 3 — anomaly surface. Implements the per-toolset
    isolation contract documented at agents.jsx:638-700: each
    bound toolset is rendered by its own ``<ToolsetSection>`` and
    a ``tools.error?.status === 500`` only collapses that panel,
    not the parent ``<AgentToolsTab>``.

    Good toolset: the built-in ``misc`` internal toolset (always
    available, returns 5 tools per primer/toolset/misc.py).
    Bad toolset: an MCP-HTTP toolset pointing at an unreachable
    URL — identical pattern to U0008's T0711 trigger.
    """
    provider_id = f"llm-u0009-{unique_suffix}"
    agent_id = f"ag-u0009-{unique_suffix}"
    bad_toolset_id = f"ts-u0009-bad-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Seed LLM (placeholder).
        r = c.post("/v1/llm_providers", json={
            "id": provider_id,
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed LLM failed: {r.text}"

        # Seed the broken MCP-HTTP toolset (T0711 trigger).
        r = c.post("/v1/toolsets", json={
            "id": bad_toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "http",
                "config": {
                    "url": "http://127.0.0.1:9999/sse",
                    "headers": {},
                },
            },
        })
        assert r.status_code == 201, f"seed bad toolset failed: {r.text}"

        # Seed agent registered with one tool from the good (misc)
        # toolset and one from the bad toolset. ``agent.tools`` holds
        # scoped ids (``<toolset_id>__<tool_name>``); the detail
        # Tools tab groups them by prefix and renders one panel per
        # source toolset.
        r = c.post("/v1/agents", json={
            "id": agent_id,
            "description": "u0009 per-toolset isolation probe",
            "model": {
                "provider_id": provider_id,
                "model_name": "fake-model",
            },
            "tools": ["misc__uuid_v4", f"{bad_toolset_id}__placeholder_tool"],
            "system_prompt": ["test"],
        })
        assert r.status_code == 201, f"seed agent failed: {r.text}"

    try:
        # Navigate directly to the Tools tab via deep-link.
        page.goto(
            f"{console_url}#/agents/{agent_id}?tab=tools",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(agent_id).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Good toolset panel renders the misc id as a header. At
        # least one of the 5 misc tools (e.g. uuid_v4) must appear
        # as a clickable row — confirms the panel rendered through
        # to ToolEntry rows.
        page.locator(".panel-h:has(.mono:text('misc'))").first.wait_for(
            state="visible", timeout=15_000,
        )
        page.get_by_text("uuid_v4", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Bad toolset panel renders the documented T0711 banner —
        # both the title ("Tools list unavailable") and the T0711
        # reference are required (same contract as U0008).
        page.get_by_text("Tools list unavailable", exact=False).first.wait_for(
            state="visible", timeout=15_000,
        )
        page.get_by_text("T0711", exact=False).first.wait_for(
            state="visible", timeout=5_000,
        )

        # Defence: the page-title is still rendered (no blank crash).
        # The agent detail h1 carries the agent id — if a render
        # error blew up the whole AgentToolsTab, the title would
        # still be visible via the page chrome, but the panels
        # wouldn't be. The asserts above already prove the panels
        # are present; this is a final structural sanity check.
        assert page.locator("h1.page-title").first.is_visible(), (
            "agent detail title disappeared after Tools tab render — "
            "page may have blanked out instead of isolating the failure"
        )

        # Defence 2: both panels are present at the same time. The
        # bad-toolset panel carries its toolset id as the .mono
        # span inside its .panel-h, just like the good one.
        page.locator(f".panel-h:has(.mono:text('{bad_toolset_id}'))").first.wait_for(
            state="visible", timeout=5_000,
        )
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            for url in (
                f"/v1/agents/{agent_id}",
                f"/v1/toolsets/{bad_toolset_id}",
                f"/v1/llm_providers/{provider_id}",
            ):
                try:
                    c.delete(url)
                except Exception:  # noqa: BLE001
                    pass
