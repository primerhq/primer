"""Platform > Toolsets, Triggers and Services: "New ..." opens the create form on the page, not a second list (ADM-06 of the 2026-10-08 admin review).

The press used to open the LEGACY list overlay (a full table with its own filter, Refresh and "+ New toolset") over the card grid, and only
a second press opened the form. The pages now host the entity's existing create dialog themselves. These journeys drive the real
Platform page: no management overlay is mounted while the form is open, Cancel returns to the card grid with the address bar untouched,
and a created toolset (or service) lands on its own detail overlay with its card in the grid behind it.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_platform_page(page, console_url: str, nav: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own
    normalisation to the Studio view), so wait for it and navigate once more if the page is not the Platform one after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id(f"nv-plat-page:{nav}")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", f"platform:{nav}")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def test_new_toolset_opens_the_create_form_with_no_list_behind_it(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    toolset_id = f"ts-adm06-{unique_suffix}"
    try:
        _open_platform_page(page, console_url, "toolsets")

        page.get_by_test_id("nv-plat-create").click()

        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text("New toolset", timeout=10_000)
        expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
        assert "overlay=" not in page.url, page.url

        dialog.get_by_placeholder("auto-generated").fill(toolset_id)
        dialog.locator("select.select").select_option("python")
        dialog.get_by_role("button", name="Create", exact=True).click()
        done = page.get_by_test_id("toolset-connect-done")
        expect(done).to_be_enabled(timeout=20_000)
        done.click()

        # The created row lands on its own detail, and its card is in the grid behind it.
        expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("nv-overlay-body")).to_contain_text(toolset_id)
        assert f"overlay=toolsets::{toolset_id}" in page.url.replace("%3A", ":"), page.url
        # The grid pages at six cards, so filter to the new id before looking for its card.
        page.get_by_test_id("nv-plat-filter").fill(toolset_id)
        expect(page.get_by_test_id(f"nv-pcard-del:{toolset_id}")).to_be_attached(timeout=15_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/toolsets/{toolset_id}")


def test_cancelling_the_new_toolset_form_leaves_the_card_grid_and_the_url_alone(page, console_url: str) -> None:
    _open_platform_page(page, console_url, "toolsets")
    before = page.url

    page.get_by_test_id("nv-plat-create").click()
    dialog = page.locator(".modal-overlay")
    expect(dialog).to_contain_text("New toolset", timeout=10_000)
    dialog.get_by_role("button", name="Cancel").click()

    expect(page.locator(".modal-overlay")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert page.url == before, (before, page.url)


def test_new_trigger_opens_the_create_wizard_with_no_list_behind_it(page, console_url: str) -> None:
    _open_platform_page(page, console_url, "triggers")
    before = page.url

    page.get_by_test_id("nv-plat-create").click()

    expect(page.get_by_test_id("tr-step-kind")).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert "overlay=" not in page.url, page.url

    page.locator(".modal-overlay").get_by_role("button", name="Cancel").click()
    expect(page.get_by_test_id("tr-step-kind")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert page.url == before, (before, page.url)


def _delete_services_named(base_url: str, name: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for row in c.get("/v1/services", params={"limit": 200}).json().get("items", []):
            if row.get("name") == name:
                c.delete(f"/v1/services/{row['id']}")


def test_new_service_opens_the_create_form_with_no_list_behind_it_and_lands_on_the_new_service(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    name = f"adm06-{unique_suffix}"
    description = f"adm06 journey {unique_suffix}"
    try:
        _open_platform_page(page, console_url, "services")

        page.get_by_test_id("nv-plat-create").click()

        expect(page.locator(".modal-overlay")).to_contain_text("New service", timeout=10_000)
        expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
        assert "overlay=" not in page.url, page.url

        page.get_by_test_id("service-name-input").fill(name)
        page.get_by_test_id("service-description-input").fill(description)
        page.get_by_test_id("service-save-btn").click()

        # The created service lands on its own detail, and its card is in the grid behind it.
        expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("nv-overlay-body")).to_contain_text(name)
        assert "overlay=services::" in page.url.replace("%3A", ":"), page.url
        page.get_by_test_id("nv-plat-filter").fill(description)
        expect(page.locator("[data-testid^='nv-pcard-del:']")).to_have_count(1, timeout=15_000)
    finally:
        _delete_services_named(base_url, name)


def test_cancelling_the_new_service_form_leaves_the_card_grid_and_the_url_alone(page, console_url: str) -> None:
    _open_platform_page(page, console_url, "services")
    before = page.url

    page.get_by_test_id("nv-plat-create").click()
    dialog = page.locator(".modal-overlay")
    expect(dialog).to_contain_text("New service", timeout=10_000)
    dialog.get_by_role("button", name="Cancel").click()

    expect(page.locator(".modal-overlay")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert page.url == before, (before, page.url)
