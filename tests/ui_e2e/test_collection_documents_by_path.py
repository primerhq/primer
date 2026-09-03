"""Path-addressed document browser + editor (Task 15).

Drives the "Browse by path" modal on a user collection's detail panel
through the full operator journey backed by the Task 11 REST surface:

  create -> list -> open -> edit -> move -> delete

Endpoints exercised through the UI:
  GET    /v1/collections/{cid}/documents?prefix=
  GET    /v1/collections/{cid}/documents?path=
  PUT    /v1/collections/{cid}/documents?path=
  POST   /v1/collections/{cid}/documents/move
  DELETE /v1/collections/{cid}/documents?path=

Like the rest of tests/ui_e2e, this is collected-then-ignored unless
``PRIMER_RUN_UI_E2E=1`` is set (see conftest.py), so it never drags the
browser stack into a plain ``uv run pytest``. It is intended for the
e2e phase against a live console.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect


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


def test_collection_document_path_browser_full_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """Create + edit + move + delete a document entirely through the
    path-addressed browser modal opened from the collection detail panel.

    Seeds an embedding provider + a (user) collection via the API, then
    selects the collection row, opens "Browse by path", and walks the
    journey, asserting the success toasts and the storage round-trips.
    """
    provider_id = f"emb-doc-{unique_suffix}"
    ssp_id = f"ssp-doc-{unique_suffix}"
    collection_id = f"col-doc-{unique_suffix}"
    # S2 addresses a document by parent + slug, and the tree route
    # enforces a [a-z0-9-]+ slug: a file extension is not part of a path
    # any more. Created at the root, so the path IS the slug.
    first_slug = "slo"
    moved_slug = "slo-renamed"
    first_path = first_slug
    moved_path = moved_slug

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

        # Semantic search needs a provider to hold the vectors; it is
        # bound below, through the search route. A self-contained local
        # lance index keeps this seed offline.
        r = c.post("/v1/ssp", json={
            "id": ssp_id,
            "provider": "lance",
            "config": {"path": f"/tmp/lance-doc-{unique_suffix}"},
        })
        assert r.status_code == 201, f"seed ssp failed: {r.text}"

        r = c.post("/v1/collections", json={
            "id": collection_id,
            "description": "task15 doc browser test",
        })
        assert r.status_code == 201, f"seed collection failed: {r.text}"

        # Bind (embedder, SSP) through the search route: S2 moved this
        # off the create body, where the old keys were silently dropped.
        r = c.put(f"/v1/collections/{collection_id}/search", json={
            "embedder": {
                "provider_id": provider_id,
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            },
            "vector_store_provider_id": ssp_id,
        })
        assert r.status_code in (200, 201, 202), f"enable search: {r.text}"

    try:
        open_legacy_route(page, console_url, "knowledge/collections")
        page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

        # Select the collection row to reveal the detail panel.
        page.locator(f"tr:has-text('{collection_id}')").first.click()

        # S2 made documents a path tree and the browser the collection's
        # own view: opening a collection IS opening its documents, so the
        # "Documents" button that used to raise a path-browser modal is
        # gone along with the modal. The tree, the grep box and the header
        # actions are all on the page.
        #
        # uiv2 Wave 2: the collection browser (KN_CollectionDetail) is now
        # ITSELF a `.modal` (it used to be a bare div hung off the list
        # page), so New-document/Move are nested modals stacked INSIDE
        # it, not the only `.modal` on screen - `.modal.first` now means
        # the outer browser, not the nested dialog. `.last` targets the
        # innermost open modal.
        browser = page.get_by_test_id("nv-overlay-body")

        # ---- create ----
        # A document is addressed by parent + slug now, not by typing a
        # whole path into one box.
        browser.get_by_role("button", name="New document").first.click()
        new_modal = page.locator(".modal").last
        new_modal.wait_for(state="visible", timeout=5_000)
        new_modal.get_by_placeholder(
            "slug (lowercase letters, digits, hyphens)",
        ).fill(first_slug)
        new_modal.get_by_placeholder("# Body").fill(
            "# SLO\n\nNinety-nine point nine percent.",
        )
        new_modal.get_by_role("button", name="Create").first.click()
        # First create indexes the doc, which lazily downloads the embedding
        # model on a cold container — allow generous time for that one-off.
        page.get_by_text("Document created", exact=False).first.wait_for(
            state="visible", timeout=45_000,
        )

        # ---- list + open ----
        browser.get_by_text(first_slug, exact=True).first.click()
        # Content pane shows the path.
        expect(
            browser.get_by_text(first_slug, exact=False).first
        ).to_be_visible()

        # ---- edit ----
        # The document body editor is a textarea carrying the shared
        # input classes, not a "textarea" class of its own.
        textarea = browser.locator("textarea.input").first
        textarea.fill("# SLO\n\nEdited body.")
        browser.get_by_role("button", name="Save", exact=True).first.click()
        page.get_by_text("Document saved", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # ---- move / rename ----
        # The header trigger is labelled exactly "Move"; it opens a nested
        # modal whose primary action is also "Move". Scope the confirm to that
        # nested modal (the one that owns the new-path input) and match exactly
        # so we don't re-target the header trigger behind the overlay.
        browser.get_by_role("button", name="Move", exact=True).first.click()
        # `.last`, not `.first`: the outer collection-browser Modal is
        # still on screen (with its own "Move" trigger button still in
        # the DOM), so `.first` would resolve to IT, not the nested
        # confirm dialog - see the comment above `browser` for why.
        move_modal = page.locator(".modal").last
        move_modal.wait_for(state="visible", timeout=5_000)
        # Parent stays root; only the slug changes, which is the rename
        # case the old single-path box expressed by retyping the whole
        # thing.
        move_modal.get_by_placeholder("New slug (optional)").fill(moved_slug)
        move_modal.get_by_role("button", name="Move", exact=True).click()
        page.get_by_text("Document moved", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # Storage round-trip: the new path resolves, the old one 404s.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(
                f"/v1/collections/{collection_id}/documents",
                params={"path": moved_path},
            )
            assert r.status_code == 200, f"moved doc not found: {r.text}"
            r = c.get(
                f"/v1/collections/{collection_id}/documents",
                params={"path": first_path},
            )
            assert r.status_code == 404

        # ---- delete ----
        browser.get_by_role("button", name="Delete").first.click()
        # Confirm through the themed window.confirm replacement. Its
        # action carries the default OK/Cancel wording, not the verb of
        # whatever asked, so it is reached by its own handle: matching
        # "Delete" here found the page's own button again and confirmed
        # nothing.
        page.get_by_test_id("dialog-confirm").click()
        page.get_by_text("Document deleted", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.get(
                f"/v1/collections/{collection_id}/documents",
                params={"path": moved_path},
            )
            assert r.status_code == 404, "document should be gone after delete"
    finally:
        _cleanup(base_url, [
            f"/v1/collections/{collection_id}",
            f"/v1/ssp/{ssp_id}",
            f"/v1/embedding_providers/{provider_id}",
        ])
