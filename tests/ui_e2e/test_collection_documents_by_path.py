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
    first_path = "concepts/slo.md"
    moved_path = "concepts/slo-renamed.md"

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
            "config": {"path": f"/tmp/lance-doc-{unique_suffix}"},
        })
        assert r.status_code == 201, f"seed ssp failed: {r.text}"

        r = c.post("/v1/collections", json={
            "id": collection_id,
            "description": "task15 doc browser test",
            "embedder": {
                "provider_id": provider_id,
                "model": "sentence-transformers/all-MiniLM-L6-v2",
            },
            "search_provider_id": ssp_id,
        })
        assert r.status_code == 201, f"seed collection failed: {r.text}"

    try:
        page.goto(
            f"{console_url}#/knowledge/collections",
            wait_until="domcontentloaded",
        )
        page.locator("h1.page-title").first.wait_for(state="visible", timeout=10_000)

        # Select the collection row to reveal the detail panel.
        page.locator(f"tr:has-text('{collection_id}')").first.click()

        # Open the path-addressed browser. For a user collection the detail
        # panel's primary button is labelled "Documents" (it opens the
        # path-browser modal); the old "Browse by path" label was removed when
        # the buttons were consolidated.
        page.get_by_role("button", name="Documents").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # ---- create ----
        modal.get_by_role("button", name="New document").first.click()
        modal.get_by_placeholder("concepts/slo.md").fill(first_path)
        modal.get_by_placeholder(
            "Document body. Stored in the content store and indexed for search.",
        ).fill("# SLO\n\nNinety-nine point nine percent.")
        modal.get_by_role("button", name="Create").first.click()
        # First create indexes the doc, which lazily downloads the embedding
        # model on a cold container — allow generous time for that one-off.
        page.get_by_text("Document created", exact=False).first.wait_for(
            state="visible", timeout=45_000,
        )

        # ---- list + open ----
        # The leaf lives under a collapsed "concepts/" folder — the file-tree
        # explorer defaults folders closed, so expand it before clicking the
        # leaf (clicking a folder row toggles it open).
        modal.get_by_text("concepts", exact=True).first.click()
        modal.locator(f"text={first_path.split('/')[-1]}").first.click()
        # Content pane shows the path + body.
        expect(modal.get_by_text(first_path, exact=False).first).to_be_visible()

        # ---- edit ----
        modal.get_by_role("button", name="Edit").first.click()
        textarea = modal.locator("textarea.textarea").first
        textarea.fill("# SLO\n\nEdited body.")
        modal.get_by_role("button", name="Save", exact=True).first.click()
        page.get_by_text("Document saved", exact=False).first.wait_for(
            state="visible", timeout=10_000,
        )

        # ---- move / rename ----
        # The header trigger is labelled exactly "Move"; it opens a nested
        # modal whose primary action is also "Move". Scope the confirm to that
        # nested modal (the one that owns the new-path input) and match exactly
        # so we don't re-target the header trigger behind the overlay.
        modal.get_by_role("button", name="Move", exact=True).first.click()
        # `.last`: the :has() selector matches both the outer browse modal
        # (which transitively contains the nested modal's input) and the nested
        # move modal itself; the nested one renders later in the DOM, so .last
        # scopes to it — otherwise the outer modal exposes two "Move" buttons
        # (the header trigger + the nested confirm).
        move_modal = page.locator(
            ".modal:has(input[placeholder='new/path.md'])"
        ).last
        move_modal.wait_for(state="visible", timeout=5_000)
        move_modal.get_by_placeholder("new/path.md").fill(moved_path)
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
        modal.get_by_role("button", name="Delete").first.click()
        # Confirm in the nested modal.
        page.get_by_role("button", name="Delete").last.click()
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
