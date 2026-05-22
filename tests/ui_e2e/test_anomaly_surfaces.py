"""Anomaly-surface regression tests.

Each documented backend anomaly that the UI is supposed to surface has
its own test here. Setup creates the backend precondition via API
(httpx), then asserts the UI renders the documented surface.

Covers:
* U0008 — T0711 anomaly banner on toolset detail Tools tab when an
  MCP-HTTP toolset points at an unreachable URL (server returns 500).
* U0012 — IC subsystem-inactive banner on /knowledge/search when the
  subsystem is OFF (no config row); sidebar IC pill reads "OFF".

UI spec §5 documents both surfaces as required.
"""

from __future__ import annotations

import time

import httpx


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


def test_u0012_knowledge_search_renders_ic_inactive_banner_when_ic_off(
    page,
    base_url: str,
    console_url: str,
) -> None:
    """U0012 — When the IC subsystem is OFF (no config row),
    /knowledge/search renders a page-level banner with a Configure
    CTA, and the sidebar IC pill reads "OFF".

    Priority 3 — anomaly surface. Setup ensures the IC config row
    is absent via API DELETE (idempotent — 204 if existed, 404 if
    not). The UI's /knowledge/search page reads
    /v1/internal_collections/config; a 404 means subsystem OFF,
    which triggers the documented banner.
    """
    # Ensure IC subsystem is OFF.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # DELETE is idempotent here — 204 or 404 are both fine.
        c.delete("/v1/internal_collections/config")

    page.goto(
        console_url + "#/knowledge/search", wait_until="domcontentloaded",
    )
    page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

    # The banner title is "Internal Collections subsystem is OFF" per
    # knowledge.jsx:647 (UI spec §5).
    page.get_by_text(
        "Internal Collections subsystem is OFF", exact=False,
    ).first.wait_for(state="visible", timeout=5_000)

    # Configure CTA visible — clicking it would navigate to the IC
    # subsystem page; we only assert presence here.
    configure_btn = page.get_by_role("button", name="Configure").first
    configure_btn.wait_for(state="visible", timeout=5_000)

    # Sidebar IC pill reads OFF. The pill is a span with class
    # nav-pill-off when subsystem is off.
    off_pill = page.locator(".nav-pill-off").first
    off_pill.wait_for(state="visible", timeout=5_000)
    assert "OFF" in off_pill.inner_text(), (
        f"sidebar IC pill expected to contain 'OFF'; "
        f"got: {off_pill.inner_text()}"
    )


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
