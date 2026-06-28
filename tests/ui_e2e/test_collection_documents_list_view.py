"""Document list views consume the path-addressed list shape.

Regression coverage for the console list views that read a user
collection's documents. The per-collection documents route was reworked
to return ``{"documents": [{path, document_id, size}]}`` (no pagination,
optional ``?prefix=``) instead of the old ``OffsetPageResponse``
``{items, total}`` shape. The two affected views are:

  * the collection detail panel's "List documents" modal, and
  * the /knowledge/documents page filtered to that collection.

Both used to read ``list.data.items`` / ``list.data.total`` and rendered
a permanently EMPTY table for non-system collections. This test seeds a
document via the path REST surface and asserts both views show the path
and open its body.

Endpoints exercised through the UI:
  GET /v1/collections/{cid}/documents          -> {documents:[{path,...}]}
  GET /v1/collections/{cid}/documents?path=    -> {document, content}

Like the rest of tests/ui_e2e, this is collected-then-ignored unless
``PRIMER_RUN_UI_E2E=1`` is set (see conftest.py), so it never drags the
browser stack into a plain ``uv run pytest``. It is intended for the
e2e phase against a live console.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-05", status="partial")


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


def test_user_collection_document_list_views_render_paths(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """A user collection's seeded document is visible + openable in both
    the "List documents" modal and the /knowledge/documents page.

    Guards the regression where these views read the stale ``items`` /
    ``total`` shape and rendered an empty table for non-system
    collections.
    """
    provider_id = f"emb-list-{unique_suffix}"
    ssp_id = f"ssp-list-{unique_suffix}"
    collection_id = f"col-list-{unique_suffix}"
    doc_path = "guides/getting-started.md"
    doc_leaf = doc_path.split("/")[-1]
    doc_body = "# Getting started\n\nList-view regression body."

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/embedding_providers", json={
            "id": provider_id,
            "provider": "huggingface",
            "models": [
                {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
            ],
            "config": {"token": "hf-placeholder"},
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed embedding provider failed: {r.text}"

        # Collections now require a SemanticSearchProvider bound at create
        # (Collection.search_provider_id). A self-contained local lance
        # index keeps this seed offline.
        r = c.post("/v1/ssp", json={
            "id": ssp_id,
            "provider": "lance",
            "config": {"path": f"/tmp/lance-list-{unique_suffix}"},
        })
        assert r.status_code == 201, f"seed ssp failed: {r.text}"

        r = c.post("/v1/collections", json={
            "id": collection_id,
            "description": "doc list-view regression test",
            "embedder": {
                "provider_id": provider_id,
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            },
            "search_provider_id": ssp_id,
        })
        assert r.status_code == 201, f"seed collection failed: {r.text}"

        # Seed one document via the path-addressed REST surface (PUT upsert).
        r = c.put(
            f"/v1/collections/{collection_id}/documents",
            params={"path": doc_path},
            json={"content": doc_body, "title": "Getting started"},
        )
        assert r.status_code in (200, 201), f"seed document failed: {r.text}"

    try:
        # ===== 1. Collection detail panel -> "List documents" modal =====
        page.goto(
            f"{console_url}#/knowledge/collections",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)
        page.locator(f"tr:has-text('{collection_id}')").first.click()

        # User-collection detail panel: the primary button is "Documents"
        # (opens the path-browser modal, which lists docs by path). "List
        # documents" only renders for system collections post-consolidation.
        page.get_by_role("button", name="Documents").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # The seeded doc lives under a collapsed "guides/" folder — the
        # file-tree explorer defaults folders closed, so the leaf is not in the
        # DOM until the folder row is expanded. Click the folder name to toggle.
        modal.get_by_text("guides", exact=True).first.click()

        # The path leaf renders as a row (NOT an empty table).
        expect(modal.get_by_text(doc_leaf, exact=False).first).to_be_visible(
            timeout=10_000,
        )
        # Clicking the row opens the body by path.
        modal.get_by_text(doc_leaf, exact=False).first.click()
        expect(modal.get_by_text(doc_path, exact=False).first).to_be_visible()
        expect(modal.get_by_text("regression body", exact=False).first).to_be_visible()

        # Close the modal.
        modal.get_by_role("button", name="Close").first.click()

        # ===== 2. /knowledge/documents filtered to the user collection =====
        page.goto(
            f"{console_url}#/knowledge/documents?collection={collection_id}",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").get_by_text(
            "Documents", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        # The path-addressed browser renders inline (same component), so the
        # "guides/" folder is collapsed here too — expand it before the leaf.
        page.get_by_text("guides", exact=True).first.click()
        expect(page.get_by_text(doc_leaf, exact=False).first).to_be_visible(
            timeout=10_000,
        )
    finally:
        _cleanup(base_url, [
            f"/v1/collections/{collection_id}",
            f"/v1/ssp/{ssp_id}",
            f"/v1/embedding_providers/{provider_id}",
        ])
