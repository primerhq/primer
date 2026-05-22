"""Empty-state rendering, toolset Tools tab deep-link, and provider
list refetch-after-create flow.

Covers:
* U0038 — Workspaces list empty state renders CTA when no rows exist.
* U0045 — Toolset Tools tab deep-link survives reload.
* U0047 — Provider list page reflects new row after modal create.
"""

from __future__ import annotations

import httpx


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# U0038 — Workspaces list empty state renders CTA when no rows exist
# ---------------------------------------------------------------------------


def test_u0038_workspaces_list_empty_state_renders_cta(
    page,
    base_url: str,
    console_url: str,
) -> None:
    """U0038 — When no workspaces exist, the Workspaces list page
    renders the empty-state panel ("No workspaces yet" head + a
    "New workspace" CTA) in place of the table.

    Priority 6 — empty-state rendering. The empty-state lives at
    workspaces.jsx:111-120 and only fires when ``items.length === 0``
    (i.e. the backend returned zero rows AND nothing was filtered
    out). Test DELETEs any pre-existing workspaces, navigates, and
    asserts the documented copy + CTA.

    Cleanup-as-precondition: the test takes ownership of removing
    any workspaces that exist before it runs. That's intrusive but
    necessary — without it the empty state can't render. After the
    test, no workspaces are added.
    """
    # Drain any pre-existing workspaces.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get("/v1/workspaces?limit=200")
        assert r.status_code == 200, f"GET /workspaces failed: {r.text}"
        for item in r.json().get("items", []):
            try:
                c.delete(f"/v1/workspaces/{item['id']}")
            except Exception:  # noqa: BLE001
                pass
        # Confirm we drained to zero.
        r = c.get("/v1/workspaces?limit=1")
        assert r.json().get("total", 0) == 0, (
            f"failed to drain workspaces to zero: {r.text}"
        )

    page.goto(f"{console_url}#/workspaces", wait_until="domcontentloaded")
    page.locator("h1.page-title").get_by_text(
        "Workspaces", exact=False,
    ).first.wait_for(state="visible", timeout=10_000)

    # Documented empty-state head copy.
    page.get_by_text("No workspaces yet", exact=False).first.wait_for(
        state="visible", timeout=10_000,
    )

    # Empty-state CTA — "New workspace" button per workspaces.jsx:118
    # (inside the empty-state panel; same label as the header button,
    # so we scope to the .empty container).
    empty_panel = page.locator(".empty:has-text('No workspaces yet')").first
    empty_panel.wait_for(state="visible", timeout=5_000)
    empty_panel.get_by_role(
        "button", name="New workspace",
    ).first.wait_for(state="visible", timeout=5_000)


# ---------------------------------------------------------------------------
# U0045 — Toolset Tools tab deep-link survives reload
# ---------------------------------------------------------------------------


def test_u0045_toolset_tools_tab_deep_link_survives_reload(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0045 — Sister of U0018 (agent Tools), U0033 (agent Config),
    U0034 (agent Metadata), U0036 (toolset Config) for the toolset
    Tools tab. Navigate to ``#/toolsets/<id>?tab=tools``, confirm
    Tools is selected, reload, confirm URL + aria-selected="true"
    on Tools survive.

    Priority 6 — routing. Completes the toolset-detail tab-routing
    contract (config / tools / sessions, per toolsets.jsx:324). The
    Tools tab is anomaly-safe with an MCP-HTTP toolset pointing at
    an unreachable URL — either the tools table OR the T0711 banner
    renders, but the page must not blank out. We don't assert
    which one renders (covered by U0008); we only assert tab state
    survives reload.
    """
    toolset_id = f"ts-u0045-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
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
        page.goto(
            f"{console_url}#/toolsets/{toolset_id}?tab=tools",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        tools_tab = page.get_by_role("tab", name="Tools").first
        tools_tab.wait_for(state="visible", timeout=5_000)
        assert tools_tab.get_attribute("aria-selected") == "true", (
            f"Tools tab not selected on initial deep-link nav; "
            f"aria-selected={tools_tab.get_attribute('aria-selected')!r}"
        )

        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        tools_tab_after = page.get_by_role("tab", name="Tools").first
        tools_tab_after.wait_for(state="visible", timeout=5_000)
        assert tools_tab_after.get_attribute("aria-selected") == "true", (
            f"Tools tab lost selected state after reload; "
            f"aria-selected={tools_tab_after.get_attribute('aria-selected')!r}"
        )
        assert "tab=tools" in page.url, (
            f"reload dropped ?tab=tools query: {page.url}"
        )

        # Defence: the page didn't blank out — either tools table or
        # T0711 banner is visible. We DON'T assert which; that's
        # U0008's job. The page title proves the chrome is rendered.
        assert page.locator("h1.page-title").first.is_visible(), (
            "toolset detail title disappeared after reload — page "
            "may have blanked out"
        )
    finally:
        _cleanup(base_url, [f"/v1/toolsets/{toolset_id}"])


# ---------------------------------------------------------------------------
# U0047 — Provider list refetches after modal create (no page reload)
# ---------------------------------------------------------------------------


def test_u0047_provider_list_reflects_new_row_after_modal_create(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0047 — Open /providers/llm, create a new LLM provider via
    the modal, after the navigate-to-detail+back, assert the new
    id is visible in the list without a page reload.

    Priority 1 — mutation feedback for the list-page surface. The
    provider list uses ``useResource("providers:llm_providers", ...)``
    with no poll; freshness is driven by the mutation's
    ``invalidates`` array (providers.jsx:336). After the modal's
    onCreate also triggers an explicit ``list.refetch()``
    (providers.jsx:117), the next mount of /providers/llm renders
    the fresh row.

    Provider choice: ``anthropic`` — its config requires only
    ``api_key`` and the "Suggest models" button populates the
    suggestedModels list directly (no live discovery call). Keeps
    the test deterministic.
    """
    provider_id = f"llm-u0047-{unique_suffix}"
    try:
        page.goto(
            f"{console_url}#/providers/llm",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )

        # Open the New LLM provider modal.
        page.get_by_role("button", name="New llm provider").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # Set the id so we can predict + assert + clean up.
        # Selector strategy: scope inputs to the modal to avoid
        # matching anything outside.
        id_input = modal.get_by_placeholder("auto-generated", exact=False).first
        id_input.fill(provider_id)

        # Select the Anthropic provider via the dropdown.
        modal.locator("select.select").first.select_option(label="Anthropic")

        # Anthropic config is a single api_key field. Fill it.
        modal.get_by_placeholder("", exact=False)  # noqa: silently no-op
        api_key_input = modal.locator("input[type=password]").first
        api_key_input.fill("sk-test-placeholder")

        # Populate models via the "Suggest models" button — anthropic
        # is non-discoverable so this loads suggestedModels directly
        # (no live call).
        modal.get_by_role("button", name="Suggest models").first.click()
        # At least one model row should now exist — wait briefly for
        # React to render the rows.
        page.wait_for_timeout(250)

        # Submit.
        modal.get_by_role("button", name="Create").first.click()

        # Wait for navigation to the detail page.
        page.wait_for_url(
            lambda url: f"#/providers/llm/{provider_id}" in url,
            timeout=10_000,
        )
        page.locator("h1.page-title").get_by_text(
            provider_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Click the "Back" button to return to the list — same
        # pattern as U0039. Scope to the page header to avoid
        # matching other Back-labelled controls.
        header_actions = page.locator(".page-header .page-actions").first
        header_actions.wait_for(state="visible", timeout=5_000)
        header_actions.get_by_role("button", name="Back").first.click()

        # On the list page now; the new row must be visible WITHOUT
        # a manual reload — the list.refetch() in onCreate
        # invalidated the cache, and re-mount of the list page
        # consumed the fresh data.
        page.wait_for_url(
            lambda url: url.rstrip("/").endswith("#/providers/llm"),
            timeout=10_000,
        )
        page.locator("h1.page-title").get_by_text(
            "LLM providers", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        page.locator(f"tr:has-text('{provider_id}')").first.wait_for(
            state="visible", timeout=10_000,
        )
    finally:
        _cleanup(base_url, [f"/v1/llm_providers/{provider_id}"])
