"""Deleting a collection or a service from its Platform card says what goes with it (ADM-31 of the 2026-10-08 admin review).

Every card asked the same "Permanently delete <id>? Referenced entities refuse deletion." even for entities that hold data. The unit tests
(``tests/ui/test_platform_delete_confirm.py``) run the prompt text through MiniRacer; these journeys drive the real card on the real server with a throwaway
collection and a throwaway service, read the dialog, confirm, and check the row is gone from the card grid and from the API.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_filtered(page, console_url: str, nav: str, query: str) -> None:
    """The Platform list pages at six cards, so filter first: the card under test is then always on page one. The shell must be mounted before the
    view hash is assigned (see test_platform_agent_delete_confirm_journey.py), so wait for it and navigate once more if the page is not the Platform one."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    box = page.get_by_test_id("nv-plat-filter")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", f"platform:{nav}")
        try:
            expect(box).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            break
        except AssertionError:
            if attempt == 2:
                raise
    box.fill(query)


def test_a_collection_card_says_its_documents_and_index_go_with_it_and_confirming_deletes_it(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    collection_id = f"del-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/collections", json={"id": collection_id, "description": "delete-consequence journey"})
        assert r.status_code == 201, r.text
    try:
        _open_filtered(page, console_url, "collections", collection_id)
        delete = page.get_by_test_id(f"nv-pcard-del:{collection_id}")
        expect(delete).to_be_visible(timeout=15_000)
        delete.click()

        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text(f"Permanently delete {collection_id}?", timeout=5_000)
        expect(dialog).to_contain_text("Every document in it")
        expect(dialog).to_contain_text("search index")
        expect(dialog).to_contain_text("cannot be undone")
        expect(dialog).not_to_contain_text("Referenced entities refuse deletion")

        dialog.get_by_role("button", name="Confirm").click()
        expect(page.get_by_test_id(f"nv-pcard-del:{collection_id}")).to_have_count(0, timeout=15_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/collections/{collection_id}").status_code == 404
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/collections/{collection_id}")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def test_a_service_card_names_the_service_and_its_url_not_its_generated_id(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    name = f"del-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/services", json={"name": name, "description": f"delete-consequence journey {unique_suffix}", "viewer_auth": "console"})
        assert r.status_code == 201, r.text
        service_id = r.json()["id"]
    try:
        _open_filtered(page, console_url, "services", f"journey {unique_suffix}")
        delete = page.get_by_test_id(f"nv-pcard-del:{service_id}")
        expect(delete).to_be_visible(timeout=15_000)
        delete.click()

        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text(f"Permanently delete {name}?", timeout=5_000)
        expect(dialog).to_contain_text("Every published version")
        expect(dialog).to_contain_text(f"/svc/{name}/ stops answering")
        expect(dialog).not_to_contain_text(service_id)

        dialog.get_by_role("button", name="Confirm").click()
        expect(page.get_by_test_id(f"nv-pcard-del:{service_id}")).to_have_count(0, timeout=15_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/services/{service_id}").status_code == 404
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            try:
                c.delete(f"/v1/services/{service_id}")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
