"""UI E2E: create a lance-backed SemanticSearchProvider via the console.

Pins the backend-aware modal contract: switching backend to "lance"
hides the Connection section, shows a Filesystem section with a
`path` field, submit succeeds, and the detail page renders the path
in the header.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


def _cleanup(base_url: str, ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for sid in ids:
            try:
                c.delete(f"/v1/ssp/{sid}")
            except Exception:  # noqa: BLE001
                pass


def test_ssp_lance_create_via_modal_journey(
    page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    ssp_id = f"ssp-lance-ui-{unique_suffix}"
    lance_path = f"/tmp/lance-ui-{unique_suffix}"

    try:
        # 1. Navigate to the SSP list page.
        page.goto(f"{console_url}#/ssp", wait_until="domcontentloaded")
        expect(page.locator("h1.page-title")).to_be_visible(timeout=15_000)

        # 2. Open the create modal — button label depends on empty vs
        # non-empty state; both contain "Semantic Search provider".
        new_btn = page.get_by_role(
            "button", name="New Semantic Search provider"
        ).or_(
            page.get_by_role("button", name="New provider")
        ).first
        expect(new_btn).to_be_visible(timeout=20_000)
        new_btn.click()
        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # 3. Default backend is pgvector — hostname field is visible.
        expect(modal.get_by_text("hostname", exact=False).first).to_be_visible()

        # 4. Switch backend to lance.
        backend_select = modal.locator("select.select").first
        backend_select.select_option("lance")

        # 5. Connection section must hide; Filesystem section appears.
        expect(modal.get_by_text("hostname", exact=False)).to_have_count(0)
        expect(modal.get_by_text("Filesystem", exact=False).first).to_be_visible()
        # The path field label has a hint appended, so use the data-testid on
        # the input rather than a text-content assertion.
        expect(modal.locator("[data-testid='ssp-lance-path']")).to_be_visible()

        # 6. HNSW knobs section is still present.
        expect(modal.get_by_text("HNSW knobs", exact=False).first).to_be_visible()

        # 7. Fill id + path.
        modal.locator("input.input.mono").nth(0).fill(ssp_id)  # id field
        # path input — use the data-testid attribute for a stable selector.
        modal.locator("[data-testid='ssp-lance-path']").fill(lance_path)

        # 8. Submit.
        submit = modal.get_by_role("button", name="Create").first
        expect(submit).to_be_enabled()
        submit.click()

        # 9. Modal closes; URL navigates to detail page.
        expect(modal).not_to_be_visible(timeout=10_000)
        page.wait_for_url(f"**/console/#/ssp/{ssp_id}**", timeout=15_000)

        # 10. Detail page header shows the path.
        expect(page.get_by_text(lance_path, exact=False).first).to_be_visible(timeout=10_000)
    finally:
        _cleanup(base_url, [ssp_id])
