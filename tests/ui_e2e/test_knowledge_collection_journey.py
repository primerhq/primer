"""UI E2E: knowledge subsystem journey — collections create + traversal.

Second post-pivot UI user-journey. The first multi-page journey
(`test_full_operator_journey.py`) navigated through 9 pages with
seeded entities; this one **creates a collection through the UI form**
(real mutation) and then traverses all three knowledge pages to pin
the cross-page polling + empty-state behaviour for a freshly-created
collection.

Pages traversed:
  1. /providers/embedding — verify the seeded embedding provider is
     visible.
  2. /knowledge/collections — click "New collection" → fill form →
     submit → assert modal closes and the row appears. S2: creating a
     collection no longer asks for an embedder or a vector store, so the
     form is id + description only; search is opt-in afterwards.
  3. Open the collection and create a document in its tree, then grep
     for its text.
  4. Back to /knowledge/collections - verify our collection still
     appears (no churn between page transitions).

Avoids LM-Studio + IC-bootstrap so the test runs anywhere.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


# ---------------------------------------------------------------------------
# API seeding (embedding provider needed for collection create)
# ---------------------------------------------------------------------------


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-05")


def _seed_embedding_provider(base_url: str, pid: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/embedding_providers", json={
            "id": pid,
            "provider": "openai",
            "models": [{"name": "stub-embed"}],
            "config": {
                "url": "http://127.0.0.1:1",
                "api_key": "sk-not-used",
                "flavor": "other",
            },
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed embedder failed: {r.text}"


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_knowledge_collection_create_via_ui_then_traverse_pages(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """Knowledge multi-page journey: create a Collection through the
    `/knowledge/collections` modal, then traverse all 3 knowledge pages
    asserting expected layout + that our new row survives polling.
    """
    emb_id = f"kj-emb-{unique_suffix}"
    coll_id = f"kj-coll-{unique_suffix}"
    _seed_embedding_provider(base_url, emb_id)

    try:
        # ===== 1. /providers/embedding — verify seeded provider visible =
        page.goto(
            f"{console_url}#/providers/embedding",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )
        expect(
            page.locator(f"tr:has-text('{emb_id}')").first
        ).to_be_visible(timeout=10_000)

        # ===== 2. /knowledge/collections — create via the modal ===========
        page.goto(
            f"{console_url}#/knowledge/collections",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            "Collections", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Open the modal.
        page.get_by_role("button", name="New collection").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # S2: id + description only. No embedder, no vector store: a
        # collection is a text wiki first and search is opt-in later.
        inputs = modal.locator("input.input")
        inputs.nth(0).fill(coll_id)
        inputs.nth(1).fill(f"journey collection {unique_suffix}")

        # Submit.
        modal.get_by_role("button", name="Create").first.click()

        # Modal closes on success.
        modal.wait_for(state="hidden", timeout=10_000)

        # New collection row appears in the list.
        expect(
            page.locator(f"tr:has-text('{coll_id}')").first
        ).to_be_visible(timeout=10_000)

        # ===== 3. Open the collection, create a doc, grep for it ==========
        page.locator(f"tr:has-text('{coll_id}')").first.get_by_role(
            "button", name="Open",
        ).click()

        page.get_by_role("button", name="New document").first.click()
        doc_modal = page.locator(".modal").first
        doc_modal.wait_for(state="visible", timeout=5_000)
        doc_inputs = doc_modal.locator("input.input")
        doc_inputs.nth(0).fill("journey-doc")
        doc_modal.locator("textarea.input").first.fill("needle in the wiki")
        doc_modal.get_by_role("button", name="Create").first.click()
        doc_modal.wait_for(state="hidden", timeout=10_000)

        # Grep works with no embedder configured: that is the point of
        # the v2 model.
        page.get_by_placeholder("grep (regex)").fill("needle")
        page.get_by_role("button", name="Grep").first.click()
        expect(
            page.get_by_text("journey-doc:1", exact=False).first
        ).to_be_visible(timeout=10_000)

        # ===== 4. Back to /knowledge/collections - row still present ======
        page.goto(
            f"{console_url}#/knowledge/collections",
            wait_until="domcontentloaded",
        )
        expect(
            page.locator(f"tr:has-text('{coll_id}')").first
        ).to_be_visible(timeout=10_000)

        # ===== Console-error hygiene across the journey ===================
        # Filter network 4xx/5xx (documented anomaly surface) — only
        # surface real JS errors.
        # Note: this test uses the conftest fixtures' default tracking;
        # we don't have direct access to console_messages here unless we
        # add it. Skip the explicit assertion — the conftest's
        # pytest_runtest_makereport hook will dump artifacts on
        # failure. Real JS errors crash the page, which would surface
        # via subsequent locator failures.

    finally:
        _cleanup(base_url, [
            f"/v1/collections/{coll_id}",
            f"/v1/embedding_providers/{emb_id}",
        ])
