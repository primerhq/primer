"""Knowledge Collections create-modal flow.

Covers:
* U0025 — New-collection modal creates row, success toast appears,
  list refreshes with the new row visible.
"""

from __future__ import annotations

import httpx


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-05", status="partial")


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# U0025 — New-collection modal happy path
# ---------------------------------------------------------------------------


def test_u0025_new_collection_modal_creates_row_and_refreshes_list(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0025 — Seed an embedding provider via API (placeholder
    HuggingFace credentials; no upstream call needed for row
    management). Open /knowledge/collections, click "New collection",
    fill the ID + pick the seeded provider+model, submit.

    Priority 1 — mutation feedback for the collection-create flow.
    Per knowledge.jsx:91-101 the modal's onCreate handler closes
    the modal, fires a success toast ("Collection created"), and
    refetches the collections list — there is no navigate-away
    (this page uses inline row-selection, not a separate detail
    route, so the new row should appear in the table after the
    refetch lands).

    Assertions:
    * modal closes,
    * "Collection created" toast visible,
    * the new collection's row appears in the list table,
    * collection landed in storage (defence).
    """
    provider_id = f"emb-u0025-{unique_suffix}"
    collection_id = f"col-u0025-{unique_suffix}"
    # Seed the embedding provider with a model so the modal's model
    # dropdown auto-selects something.
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/embedding_providers", json={
            "id": provider_id,
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
        assert r.status_code == 201, f"seed embedding provider failed: {r.text}"

    try:
        page.goto(
            f"{console_url}#/knowledge/collections",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )

        # Open the New collection modal.
        page.get_by_role("button", name="New collection").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # Fill the ID input — first input. Description is required
        # by the backend (Collection schema enforces a non-null
        # description), so fill the second input too.
        modal.locator("input.input").nth(0).fill(collection_id)
        modal.locator("input.input").nth(1).fill("u0025 test collection")

        # The provider + model dropdowns auto-select on mount via
        # the modal's useEffect (knowledge.jsx:272-283). With only
        # one seeded provider + one model, the defaults work; we
        # set explicitly for determinism.
        modal.locator("select.select").nth(0).select_option(
            value=provider_id,
        )
        modal.locator("select.select").nth(1).select_option(
            value="sentence-transformers/all-MiniLM-L6-v2",
        )

        # Submit.
        modal.get_by_role("button", name="Create").first.click()

        # Modal closes.
        modal.wait_for(state="hidden", timeout=10_000)

        # Success toast.
        page.get_by_text(
            "Collection created", exact=False,
        ).first.wait_for(state="visible", timeout=5_000)

        # New row appears in the table after the list.refetch().
        page.locator(f"tr:has-text('{collection_id}')").first.wait_for(
            state="visible", timeout=10_000,
        )

        # Defence: storage round-trip.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/collections/{collection_id}")
            assert r.status_code == 200, (
                f"collection {collection_id!r} not in storage: "
                f"{r.status_code}: {r.text}"
            )
            assert r.json()["id"] == collection_id
            assert r.json()["embedder"]["provider_id"] == provider_id
    finally:
        _cleanup(base_url, [
            f"/v1/collections/{collection_id}",
            f"/v1/embedding_providers/{provider_id}",
        ])
