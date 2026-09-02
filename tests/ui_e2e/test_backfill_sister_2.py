"""Backfill #2: modal overlay click + embedding provider invalidate + sidebar workers count.

Covers backlog items:

* U0097 — Modal overlay-click dismisses any open create modal (sister
  of U0044's ESC dismiss). Pins shared.jsx:112's
  ``<div className="modal-overlay" onClick={onClose}>`` against the
  inner ``.modal`` div's ``stopPropagation`` (line 113).
* U0098 — Embedding provider detail Invalidate button toasts "Cache
  dropped" + preserves the row (sister of U0091 for LLM providers).
  All provider families share ProviderDetailHeader, so the same
  contract should hold for the embedding family.
* U0099 — Sidebar Workers nav count matches GET /v1/workers items
  length on initial render (sister of U0002 sessions count + U0024
  workspaces count — Workers is the third polled count per
  the console shell:21 NAV entry + line 123 ``counts.workers``).
"""

from __future__ import annotations


import httpx
from playwright.sync_api import expect
from tests.ui_e2e._shell_helpers import open_legacy_route
from tests.ui_e2e._studio_helpers import open_provider_catalog


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0097 — Modal overlay click dismisses
# ===========================================================================


def test_u0097_modal_overlay_click_dismisses_create_modal(
    page, base_url, console_url,
) -> None:
    """U0097 — Sister of U0044 (modal ESC dismiss). Open any create
    modal (Agents → "New agent"), click on the ``.modal-overlay``
    background (outside the modal box), assert the modal closes
    without firing the create POST.

    Pins shared.jsx:112's overlay onClick={onClose} against the
    .modal div's stopPropagation guard (line 113) — clicks INSIDE
    the modal must not dismiss, clicks on the overlay around it
    must.
    """
    delete_or_post_calls = {"count": 0}

    def _on_agents_mutate(route):
        # Should NEVER fire — overlay dismiss must not trigger create.
        method = route.request.method
        if method in ("POST", "PUT", "DELETE"):
            delete_or_post_calls["count"] += 1
            route.fulfill(status=500, content_type="application/json", body="{}")
        else:
            route.continue_()

    page.route("**/v1/agents", _on_agents_mutate)

    try:
        open_legacy_route(page, console_url, "agents")

        # Open New agent modal.
        page.get_by_role(
            "button", name="New agent", exact=False,
        ).first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # Click on the overlay outside the modal box. The overlay
        # covers the full viewport; the modal box is centered. Click
        # at viewport (10, 10) which is guaranteed to be on the
        # overlay, not on the modal.
        overlay = page.locator(".modal-overlay").first
        overlay.wait_for(state="visible", timeout=3_000)
        # Use position to click in the top-left corner of the
        # overlay (outside the centered modal).
        overlay.click(position={"x": 10, "y": 10})

        # Modal closes.
        page.wait_for_timeout(300)
        assert page.locator(".modal").count() == 0, (
            "modal didn't dismiss on overlay click"
        )

        # No POST/PUT/DELETE fired.
        assert delete_or_post_calls["count"] == 0, (
            f"overlay click triggered a mutation; "
            f"calls={delete_or_post_calls['count']}"
        )
    finally:
        page.unroute("**/v1/agents")


# ===========================================================================
# U0098 — Embedding provider Invalidate toasts + preserves row
# ===========================================================================


def test_u0098_embedding_provider_invalidate_toasts_and_preserves_row(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0098 — Sister of U0091 (LLM provider Invalidate). Seed an
    embedding provider, open its card's edit overlay, click
    "Invalidate model cache" → POST /v1/embedding_providers/{id}/invalidate
    fires → ``kind=success`` toast "Cache dropped" appears per
    PC_InvalidateAction (provider-form.jsx); the row remains GET-able
    via API (invalidate drops the cached adapter, not the row).

    RETARGET (01a063ab, designer reconciliation): same relocation as
    U0091 - Invalidate moved off the card footer into the edit overlay
    (PC_InvalidateAction), so this reaches it via the card's Open
    button rather than a direct instance-id route (which only ever set
    instanceId for the model-profiles panel, not an auto-opened edit
    form, on this class either before or after this change).
    """
    pid = f"emb-u98-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/embedding_providers", json={
            "id": pid,
            "provider": "huggingface",
            "models": [
                {
                    "name": "sentence-transformers/all-MiniLM-L6-v2",
                    "dim": 384,
                },
            ],
            "config": {"token": "hf-placeholder"},
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, r.text
    cleanup_urls = [f"/v1/embedding_providers/{pid}"]
    try:
        open_provider_catalog(page, console_url, cls="embedding")
        page.click(f'[data-testid="provider-card-open-{pid}"]')
        form = page.get_by_test_id("provider-form-embedding_providers")
        form.wait_for(state="visible", timeout=15_000)

        inv = form.get_by_test_id("provider-form-invalidate-action")
        # Bumped wait vs U0091's LLM variant, same margin as before this
        # retarget, to give the form's own fetches a beat to settle.
        inv.wait_for(state="visible", timeout=20_000)
        inv.click()

        expect(
            page.get_by_text("Cache dropped", exact=False).first
        ).to_be_visible(timeout=5_000)

        # Row still exists via API.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/embedding_providers/{pid}")
            assert r.status_code == 200, r.text
            assert r.json()["id"] == pid
    finally:
        _cleanup(base_url, cleanup_urls)


# ===========================================================================
# U0099 — Sidebar Workers nav count matches /v1/workers items length
# ===========================================================================
