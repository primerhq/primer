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
    ``api_key``, so the form has nothing else to fill. An LLM provider
    no longer carries a models list (its ModelProfile rows are the
    registry), so creating one needs no model step at all; that is the
    behaviour this now asserts.
    """
    provider_id = f"llm-u0047-{unique_suffix}"
    try:
        # RETARGET (01a063ab, designer reconciliation): Register now
        # lists the active class's KINDS directly (PC_RegisterDropdown) -
        # picking "anthropic" there opens the form with kind already
        # preselected, no in-form re-pick left to do. The round trip out
        # to a detail page and back is still gone, and the list still has
        # to show the new row in place, just now behind Register -> kind
        # -> Save.
        open_legacy_route(page, console_url, "providers/llm")
        page.get_by_test_id("provider-register-toggle").click()
        page.get_by_test_id("provider-register-kind-anthropic").click()
        form = page.get_by_test_id("provider-form-llm_providers")
        form.wait_for(state="visible", timeout=15_000)

        form.locator('[data-field="id"] input').fill(provider_id)
        api_key_input = form.locator("input[type=password]").first
        if api_key_input.count():
            api_key_input.fill("sk-test-placeholder")

        # No model step: an LLM provider has no models[] to declare, so
        # Save must already be enabled. This is the regression guard --
        # the old form gated submit on models.length > 0, which made LLM
        # providers uncreatable once the field was removed.
        from playwright.sync_api import expect

        save_btn = form.get_by_test_id("provider-form-save")
        expect(save_btn).to_be_enabled()
        save_btn.click()

        # The row appears in the instance list beside the form, with no
        # reload and no navigation. The list paginates at 25 with no
        # search field (provider-catalog.jsx's PC_InstanceList) and the
        # shared stack legitimately accumulates fixture residue across
        # rounds (by design), which can push the new row past page 1 -
        # page forward within the SAME already-open list (no reload, no
        # route change) rather than assuming page 1.
        row = page.get_by_test_id("provider-instances-llm").get_by_text(
            provider_id, exact=True,
        )
        next_btn = page.get_by_test_id("pager-next")
        for _ in range(50):
            if row.count() > 0 or next_btn.is_disabled():
                break
            next_btn.click()
            page.wait_for_timeout(300)
        expect(row).to_be_visible(timeout=15_000)
    finally:
        _cleanup(base_url, [f"/v1/llm_providers/{provider_id}"])
