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
  chrome.jsx:21 NAV entry + line 123 ``counts.workers``).
"""

from __future__ import annotations

import time

import httpx
from playwright.sync_api import expect


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
        page.goto(f"{console_url}#/agents", wait_until="domcontentloaded")
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

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
    embedding provider; navigate to ``#/providers/embedding/<id>``;
    click "Invalidate" → POST /v1/embedding_providers/{id}/invalidate
    fires → ``kind=info`` toast "Cache dropped" appears per
    providers.jsx:593; the row remains GET-able via API
    (invalidate drops the cached adapter, not the row).
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
        page.goto(
            f"{console_url}#/providers/embedding/{pid}",
            wait_until="domcontentloaded",
        )
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        inv = page.get_by_role(
            "button", name="Invalidate", exact=True,
        ).first
        # Embedding provider detail page can take a while to fully
        # render (detail + models fetches must both settle before the
        # Header switches from loading-branch to success-branch with
        # the Invalidate button). Bump the wait vs U0091's LLM
        # variant.
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


def test_u0099_sidebar_workers_count_matches_api(
    page, base_url, console_url,
) -> None:
    """U0099 — Sister of U0002 (sessions count) + U0024 (workspaces
    count) for the Workers nav row. Sidebar's `Workers` entry
    declares ``count: "workers"`` (chrome.jsx:63) and chrome.jsx:123
    populates ``counts.workers = workers.data?.items?.length``.

    Fetch /v1/workers via the API to compute the expected count,
    then assert the sidebar's Workers row .count element matches
    within ~15s (real poll cadence 5s per chrome.jsx:114).
    """
    # Compute expected count from the API.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get("/v1/workers")
        assert r.status_code == 200, r.text
        expected = len(r.json().get("items", []))
    # primer-app runs with --run-worker so at least 1 worker is alive.
    assert expected >= 1, (
        f"expected at least 1 registered worker; API returned {expected}"
    )

    page.goto(f"{console_url}#/", wait_until="domcontentloaded")
    workers_nav = page.locator(
        ".nav-item:has(.label:text('Workers'))"
    ).first
    workers_nav.wait_for(state="visible", timeout=10_000)

    def _read_count() -> int | None:
        count_el = workers_nav.locator(".count").first
        if count_el.count() == 0:
            return None
        txt = (count_el.text_content() or "").strip()
        try:
            return int(txt)
        except ValueError:
            return None

    deadline = time.monotonic() + 15.0
    actual: int | None = None
    while time.monotonic() < deadline:
        actual = _read_count()
        if actual == expected:
            break
        page.wait_for_timeout(250)
    assert actual == expected, (
        f"sidebar Workers count {actual!r} != API expected {expected} "
        "within 15s"
    )
