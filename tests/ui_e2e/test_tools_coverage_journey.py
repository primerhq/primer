"""Tools coverage on shipped desktop/mobile JSX, owned by the UI CI lane.

The shared server has auth disabled. The non-admin rendering case supplies
an explicit response fixture in its fresh browser context; real user/admin
authorization and unchanged picker IDs are tested in test_tools_catalogue_m6_coverage.
The admin and mobile cases read the live catalogue without intercepting it.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._a11y import unnamed_controls
from tests.ui_e2e._shell_helpers import default_wid, open_view
from tests.ui_e2e.test_platform_tools_page_journey import SLOW, _open_tools_page

pytestmark = smk("SMK-UI-03", status="partial")


def _find(page: Page, scoped: str):
    page.get_by_test_id("nv-plat-filter").fill(scoped)
    card = page.get_by_test_id(f"pk-card:{scoped}")
    expect(card).to_be_visible(timeout=SLOW)
    return card


def test_admin_tools_show_mapping_reference_default_policy_and_reviewed_skips(page: Page, console_url: str):
    _open_tools_page(page, console_url)
    card = _find(page, "internal_collections__put_config")
    expect(card).to_contain_text("generated")
    expect(card).to_contain_text("PUT /v1/internal_collections/config")
    expect(card).to_contain_text("reference/tools/internal_collections/put_config")
    expect(card).to_contain_text("default policy: required")
    expect(card).to_contain_text("MCP exposure")
    for scoped in ("crud__create_agent", "system__create_agent"):
        expect(_find(page, scoped)).to_contain_text("POST /v1/agents")
    skips = page.get_by_test_id("nv-plat-skips")
    expect(skips.get_by_role("heading", name="Reviewed skips")).to_be_visible()
    expect(skips.locator("[data-testid^='nv-plat-skip:']")).to_have_count(8)
    expect(page.get_by_test_id("nv-plat-coverage-counts")).to_contain_text("404 operations")
    expect(page.get_by_test_id("nv-plat-refresh")).to_have_text("Refresh", timeout=SLOW)
    assert unnamed_controls(page, '[data-testid="nv-plat-page:tools"]') == []


def test_nonadmin_response_fixture_renders_existing_item_without_hidden_operation(page: Page, console_url: str):
    body = {
        "items": [{
            "id": "internal_collections__put_config", "description": "Configure internal collections",
            "origin": "generated", "reference_path": "reference/tools/internal_collections/put_config",
            "default_approval": "required", "exposure": None,
        }],
        "skips": [{"operation_id": "sign_in", "method": "POST", "path": "/v1/auth/login", "reason": "human sign in"}],
    }
    page.route("**/v1/tools/catalogue", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(body),
    ))
    _open_tools_page(page, console_url)
    card = _find(page, "internal_collections__put_config")
    expect(card).to_contain_text("generated")
    expect(card).not_to_contain_text("PUT /v1/internal_collections/config")
    expect(card).not_to_contain_text("MCP exposure")
    expect(page.get_by_test_id("nv-plat-coverage-counts")).to_have_count(0)
    expect(page.get_by_test_id("nv-plat-skip:sign_in")).to_contain_text("POST /v1/auth/login - human sign in")


def test_mobile_uses_the_same_coverage_facts_and_reviewed_skips(page: Page, console_url: str):
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    open_view(page, console_url, default_wid(console_url), "platform:tools")
    marker = page.get_by_test_id("nv-mob-plat-page:tools")
    expect(marker).to_be_visible(timeout=30_000)
    page.get_by_test_id("nv-mob-plat-filter").fill("internal_collections__put_config")
    row = page.get_by_test_id("nv-mob-plat-row:internal_collections__put_config")
    expect(row).to_be_visible(timeout=SLOW)
    expect(page.get_by_test_id("nv-plat-skips").locator("[data-testid^='nv-plat-skip:']")).to_have_count(8)
    row.click()
    sheet = page.get_by_test_id("nv-mob-fact-body")
    expect(sheet).to_contain_text("generated")
    expect(sheet).to_contain_text("PUT /v1/internal_collections/config")
    expect(sheet).to_contain_text("reference/tools/internal_collections/put_config")
    expect(sheet).to_contain_text("default policy: required")
    assert unnamed_controls(page, '[data-testid="nv-mob-fact-body"]') == []
