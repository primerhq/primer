"""Knowledge Collections create-modal flow.

Covers:
* U0025 — New-collection modal creates row, success toast appears, the
  detail overlay for the new collection opens.

01a08b77: this test used to bind `page.locator(".modal").first` before
clicking Create, then wait for it to become hidden. Playwright locators
are lazy - `.first` re-resolves at every poll, it is not pinned to the
DOM node that existed when the variable was assigned. Creating a
collection closes the create modal AND immediately opens the detail
overlay, which renders through the SAME shared `Modal` component
(ui/components/shared.jsx) with the SAME unconditional `className=
"modal"`. So the locator was structurally incapable of distinguishing
"the create modal closed" from "a different, legitimate modal is now
open" - it just sees `.modal` continuously satisfied by two different,
both-correct elements in quick succession. Confirmed via two
independent CI failure screenshots (both showed the detail overlay
correctly open with the right collection id, no create modal in sight,
moments after the "still visible" timeout fired) - the create modal was
never stuck; the test could never observe it closing. Scope any new
`.modal` assertion in this file (or written against it) to the specific
modal's own content (e.g. `has_text=`), not the bare class.
"""

from __future__ import annotations

import httpx


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route
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
    Creating a collection closes the create modal, fires a success
    toast ("Collection created"), and addresses straight into the new
    collection's detail overlay (there is no "stay on the list" path -
    the overlay leaves the list and shows the document browser).

    Assertions:
    * the CREATE modal specifically closes (01a08b77: scoped by its own
      title text, not a bare `.modal` locator - the detail overlay this
      flow opens next renders through the SAME shared Modal component
      with the SAME `.modal` class, so an unscoped locator is
      structurally unable to tell "the create modal closed" from "a
      different, legitimate modal is now open"; see the module-level
      comment above for the full mechanism),
    * "Collection created" toast visible,
    * the detail overlay opens showing the new collection's id,
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
        open_legacy_route(page, console_url, "knowledge/collections")
        page.locator("h1.page-title").first.wait_for(
            state="visible", timeout=10_000,
        )

        # Open the New collection modal. Scoped by its own title text, not
        # a bare `.modal` class - the detail overlay this flow opens next
        # (right after submit) renders through the SAME shared Modal
        # component with the SAME class (see the module docstring above),
        # so an unscoped locator would resolve to whichever modal is
        # currently in the DOM, not specifically this one.
        page.get_by_role("button", name="New collection").first.click()
        modal = page.locator(".modal", has_text="New collection")
        modal.wait_for(state="visible", timeout=5_000)

        # Fill the ID input — first input. Description is required
        # by the backend (Collection schema enforces a non-null
        # description), so fill the second input too.
        modal.locator("input.input").nth(0).fill(collection_id)
        modal.locator("input.input").nth(1).fill("u0025 test collection")

        # No provider or model dropdowns: S2 took the vector-space
        # fields off collection create and gave them their own route
        # (PUT /collections/{id}/search), so a collection is created
        # with an id and a description and is grep-only until search is
        # turned on. The modal has exactly those two inputs.

        # Submit.
        modal.get_by_role("button", name="Create").first.click()

        # The CREATE modal specifically closes. Scoped by has_text=
        # above, so this stays true even though a DIFFERENT modal (the
        # detail overlay, asserted below) opens right after - it never
        # contains "New collection" text, so it can't satisfy this
        # locator and mask the create modal's own closing.
        modal.wait_for(state="hidden", timeout=10_000)

        # Success toast.
        page.get_by_text(
            "Collection created", exact=False,
        ).first.wait_for(state="visible", timeout=5_000)

        # Creating a collection opens it. The overlay leaves the list and
        # shows the new collection's document browser, so what proves the
        # create landed is the breadcrumb naming it, not a row in a table
        # that is no longer on screen.
        page.get_by_test_id("nv-overlay-body").get_by_text(
            collection_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)
        assert "overlay=collections" in page.url, (
            f"expected to land in the collections overlay, got {page.url}"
        )

        # Defence: storage round-trip.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(f"/v1/collections/{collection_id}")
            assert r.status_code == 200, (
                f"collection {collection_id!r} not in storage: "
                f"{r.status_code}: {r.text}"
            )
            assert r.json()["id"] == collection_id
            # S2: the console's create form still posts the pre-v2 embedder
            # trio, which the server ignores, so the row comes back
            # grep-only. Task 21 (S2 P4) rebuilds the form onto the search
            # block; this assertion tightens to a real search block then.
            assert r.json()["search"] is None
    finally:
        _cleanup(base_url, [
            f"/v1/collections/{collection_id}",
            f"/v1/embedding_providers/{provider_id}",
        ])
