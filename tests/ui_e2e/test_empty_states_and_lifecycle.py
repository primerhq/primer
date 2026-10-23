"""Empty-state rendering, toolset Tools tab deep-link, and provider
list refetch-after-create flow.

Covers:
* U0038 — Workspaces list empty state renders CTA when no rows exist.
* U0045 — Toolset Tools tab deep-link survives reload.
* U0047 — Provider list page reflects new row after modal create.
"""

from __future__ import annotations

import httpx
from tests.ui_e2e._shell_helpers import open_legacy_route
from tests.ui_e2e._studio_helpers import open_provider_catalog


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


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
        # allow_unreachable: this lifecycle test seeds an unreachable MCP-HTTP
        # toolset on purpose; opt out of the create-time connectivity probe.
        r = c.post("/v1/toolsets?allow_unreachable=true", json={
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
        open_legacy_route(page, console_url, f"toolsets/{toolset_id}", tab="tools")
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
        # The tab travels in the overlay target's section slot now.
        assert f"overlay=toolsets:tools:{toolset_id}" in page.url, (
            f"reload dropped the tools tab: {page.url}"
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
    mock_llm_lan,
) -> None:
    """U0047: Register an LLM provider through the overlay with mock_llm_lan,
    import 1 model, and assert the new card is visible.
    """
    from playwright.sync_api import expect

    _registry, mock_base_url = mock_llm_lan
    provider_id = f"llm-u0047-{unique_suffix}"
    try:
        open_provider_catalog(page, console_url, cls="llm")
        page.get_by_test_id("pc-register-open").click()
        overlay = page.get_by_test_id("pc-register")
        overlay.wait_for(state="visible", timeout=15_000)

        overlay.get_by_test_id("pc-field:id").fill(provider_id)
        overlay.get_by_test_id("pc-field:url").fill(mock_base_url)

        next_btn = overlay.get_by_test_id("pc-register-next")
        expect(next_btn).to_be_enabled(timeout=10_000)
        next_btn.click()

        models_view = overlay.locator('[data-testid="pc-llm-models"]')
        models_view.wait_for(state="visible", timeout=15_000)

        first_box = overlay.locator('[data-testid^="pc-import:"]').first
        first_box.check()
        ctx_input = overlay.locator('[data-testid^="pc-import-context:"]').first
        if ctx_input.input_value() == "":
            ctx_input.fill("8192")

        submit = overlay.get_by_test_id("pc-register-submit")
        expect(submit).to_be_enabled(timeout=10_000)
        submit.click()

        expect(overlay).not_to_be_visible(timeout=15_000)
        card = page.get_by_test_id(f"pk-card:llm:{provider_id}")
        expect(card).to_be_visible(timeout=15_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/llm_providers/{provider_id}")
        _cleanup(base_url, [f"/v1/llm_providers/{provider_id}"])
