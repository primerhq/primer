"""Platform > Triggers > New trigger: a refused schedule is shown on the schedule step, where it can be fixed (ADM-21 and ADM-22 of the 2026-10-08 admin review).

The wizard carried ``bad cron`` past step 2, let the operator name the trigger, and only after Create on step 3 (where the cron cannot be edited) showed a generic "Create failed"
banner. This journey drives the real wizard against the real server:

* a cron of the wrong shape stays on step 2 with the reason under the field and no request sent;
* a cron of the right shape but with invalid values (``60 * * * *``) is only known to the server: Create on step 3 answers 422 ``cron_invalid``, and the wizard goes back to step 2
  with the server's message under the cron field, with what was typed on step 3 kept, and nothing created;
* after fixing the cron the same name goes through and the trigger exists;
* the slug hint on step 3 says the rule in words, not ``^[A-Z][A-Z0-9-]63$``.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_triggers(page: Page, console_url: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation), so wait for it and
    navigate once more if the Platform page is not there after a short wait."""
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


def _triggers_named(base_url: str, slug: str) -> list[dict]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return [t for t in c.get("/v1/triggers", params={"limit": 200}).json().get("items", []) if t.get("slug") == slug]


def test_a_refused_schedule_is_shown_on_the_schedule_step_and_step_three_is_kept(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    slug = f"adm21-{unique_suffix}"
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.rstrip("/").endswith("/v1/triggers") else None)
    try:
        _open_triggers(page, console_url)
        page.get_by_test_id("nv-plat-create").click()
        expect(page.get_by_test_id("tr-step-kind")).to_be_visible(timeout=10_000)
        page.locator("#tr-kind-select").select_option("scheduled")
        page.locator(".modal-overlay").get_by_role("button", name="Next").click()
        expect(page.get_by_test_id("tr-step-scheduled")).to_be_visible()
        cron = page.locator("#tr-cron")

        # The wrong shape is named on step 2, under the field, and the wizard stays there.
        cron.fill("bad cron")
        page.locator(".modal-overlay").get_by_role("button", name="Next").click()
        expect(page.get_by_test_id("tr-cron-error")).to_contain_text("5 fields")
        expect(page.get_by_test_id("tr-cron-error")).to_contain_text("This one has 2")
        expect(page.get_by_test_id("tr-step-scheduled")).to_be_visible()
        expect(page.get_by_test_id("tr-step-meta")).to_have_count(0)
        assert posts == []

        # Editing the field takes the message away.
        cron.fill("60 * * * *")
        expect(page.get_by_test_id("tr-cron-error")).to_have_count(0)

        # The right shape with invalid values is only known to the server: step 3 is reached, and the 422 sends the wizard back to step 2.
        page.locator(".modal-overlay").get_by_role("button", name="Next").click()
        expect(page.get_by_test_id("tr-step-meta")).to_be_visible()
        # ADM-22: the slug hint states the rule in words.
        hint = page.locator("label[for='tr-slug'] .hint")
        expect(hint).to_contain_text("lowercase letters, digits and hyphens")
        assert "63$" not in hint.inner_text() and "^[" not in hint.inner_text(), hint.inner_text()
        page.locator("#tr-name").fill(slug)
        expect(page.locator("#tr-slug")).to_have_value(slug)
        page.locator(".modal-overlay").get_by_role("button", name="Create", exact=True).click()

        expect(page.get_by_test_id("tr-step-scheduled")).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("tr-cron-error")).to_contain_text("60 * * * *")
        assert len(posts) == 1, posts
        assert _triggers_named(base_url, slug) == [], "a refused schedule must not leave a trigger behind"

        # Fixing the cron and going on finds step 3 as it was left, and the same name goes through.
        cron.fill("0 9 * * 1")
        page.locator(".modal-overlay").get_by_role("button", name="Next").click()
        expect(page.get_by_test_id("tr-step-meta")).to_be_visible()
        expect(page.locator("#tr-name")).to_have_value(slug)
        expect(page.locator("#tr-slug")).to_have_value(slug)
        page.locator(".modal-overlay").get_by_role("button", name="Create", exact=True).click()
        expect(page.get_by_test_id("tr-step-meta")).to_have_count(0, timeout=15_000)
        created = _triggers_named(base_url, slug)
        assert len(created) == 1 and created[0]["config"]["cron"] == "0 9 * * 1", created
    finally:
        for row in _triggers_named(base_url, slug):
            with httpx.Client(base_url=base_url, timeout=30.0) as c:
                c.delete(f"/v1/triggers/{row['id']}")
