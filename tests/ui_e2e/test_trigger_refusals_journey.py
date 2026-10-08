"""A refused trigger write says what to do about it, and never shows the code (ticket 01a11bf7-15b7; the lead's ruling: a snake_case code is never shown in a banner title).

Two real refusals from the real server:

* a duplicate slug in the create wizard: the banner on step 3 is titled "Create failed" (no code in it), carries the server's own message and one sentence on what to do;
* Fire now on a trigger that was deleted underneath the open detail page: the banner is titled "Fire failed" and carries the server's explanation, not the bare HTTP title "Not Found" the
  fire block used to show because it never read ``detail``.
"""

from __future__ import annotations

import datetime as dt

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route, open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _triggers_named(base_url: str, slug: str) -> list[dict]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return [t for t in c.get("/v1/triggers", params={"limit": 200}).json().get("items", []) if t.get("slug") == slug]


def _remove(base_url: str, slug: str) -> None:
    for row in _triggers_named(base_url, slug):
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/triggers/{row['id']}")


def _open_triggers(page: Page, console_url: str) -> None:
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id("nv-plat-page:triggers")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", "platform:triggers")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def test_a_duplicate_slug_says_so_and_what_to_do_with_a_plain_title(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"rf-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "webhook"}, "enabled": True})
        assert r.status_code == 201, r.text
    try:
        _open_triggers(page, console_url)
        page.get_by_test_id("nv-plat-create").click()
        expect(page.get_by_test_id("tr-step-kind")).to_be_visible(timeout=10_000)
        modal = page.locator(".modal-overlay")
        page.locator("#tr-kind-select").select_option("webhook")
        modal.get_by_role("button", name="Next").click()
        expect(page.get_by_test_id("tr-step-webhook")).to_be_visible()
        modal.get_by_role("button", name="Next").click()
        expect(page.get_by_test_id("tr-step-meta")).to_be_visible()

        page.locator("#tr-name").fill(slug)
        expect(page.locator("#tr-slug")).to_have_value(slug)
        modal.get_by_role("button", name="Create", exact=True).click()

        banner = page.get_by_test_id("tr-step-meta")
        expect(banner).to_contain_text("already in use", timeout=15_000)
        expect(banner).to_contain_text("Choose a different slug.")
        shown = banner.inner_text()
        assert "Create failed" in shown and "trigger_slug_conflict" not in shown and "Create failed (" not in shown, shown
    finally:
        _remove(base_url, slug)


def test_a_failed_fire_shows_the_servers_explanation_and_a_plain_title(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"rf-fire-{unique_suffix}"
    when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).isoformat()
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "delayed", "fire_at": when}, "enabled": True})
        assert r.status_code == 201, r.text
        trigger_id = r.json()["id"]
    try:
        open_legacy_route(page, console_url, f"triggers/{trigger_id}")
        fire = page.get_by_role("button", name="Fire now")
        expect(fire).to_be_visible(timeout=20_000)

        # The trigger is deleted underneath the open page; the page has not noticed yet.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.delete(f"/v1/triggers/{trigger_id}").status_code in (200, 204)
        fire.click()

        banner = page.locator(".banner, [role='alert']").filter(has_text="Fire failed")
        expect(banner.first).to_be_visible(timeout=15_000)
        shown = banner.first.inner_text()
        assert "was not found" in shown and "Refresh the list." in shown, shown
        assert "trigger_not_found" not in shown and "Fire failed (" not in shown, shown
    finally:
        _remove(base_url, slug)
