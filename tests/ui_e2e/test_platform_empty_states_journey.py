"""Platform lists tell an empty list from a filter that hides every row, and count in the user's words (finding M3 of the lead's sweep).

An EMPTY Platform list said "Nothing here yet." even while a filter was hiding rows (and offered "New ..." as if nothing existed); on mobile an
empty list said "No matches." with no filter at all; the header counted "1 entity" / "3 entities". These journeys drive the real desktop
Platform page and, at a phone viewport, the mobile Platform list (More tab): Triggers, which the lane's fresh instance has none of, and
Toolsets with one seeded through the API.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_mobile_platform_nav, open_view

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


def test_an_empty_list_says_it_is_empty_with_or_without_a_filter(page, base_url: str, console_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        existing = c.get("/v1/triggers", params={"limit": 1}).json()
    if existing.get("items"):
        import pytest

        pytest.skip("this instance already has triggers, so the list is not empty")
    _open_platform_page(page, console_url, "triggers")

    expect(page.get_by_test_id("nv-plat-count")).to_have_text("0 triggers", timeout=15_000)
    expect(page.get_by_test_id("nv-plat-empty")).to_contain_text("No triggers yet.")
    expect(page.get_by_test_id("nv-plat-empty").get_by_role("button", name="New trigger")).to_be_visible()

    # Nothing to match against: a filter must not turn "empty" into "no match".
    page.get_by_test_id("nv-plat-filter").fill("zzz")
    expect(page.get_by_test_id("nv-plat-empty")).to_contain_text("No triggers yet.")
    expect(page.get_by_test_id("nv-plat-empty")).not_to_contain_text("match")
    # ... and there is still nothing to clear: the way out is to create one, not a "Clear filter" for rows that do not exist.
    expect(page.get_by_test_id("nv-plat-empty").get_by_role("button", name="New trigger")).to_be_visible()
    expect(page.get_by_test_id("nv-plat-empty").get_by_role("button", name="Clear filter")).to_have_count(0)


def test_a_filter_that_hides_every_card_says_nothing_matches_and_clears(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    toolset_id = f"ts-m3-{unique_suffix}"
    body = {"id": toolset_id, "provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["echo"]}}}
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.post("/v1/toolsets", json=body).status_code == 201
        _open_platform_page(page, console_url, "toolsets")
        page.get_by_test_id("nv-plat-filter").fill(toolset_id)
        expect(page.get_by_test_id("nv-pcard-del:" + toolset_id)).to_be_attached(timeout=15_000)

        page.get_by_test_id("nv-plat-filter").fill("zzz-no-such-toolset")

        empty = page.get_by_test_id("nv-plat-empty")
        expect(empty).to_contain_text('No toolsets match "zzz-no-such-toolset".', timeout=10_000)
        expect(empty).not_to_contain_text("yet")
        expect(empty.get_by_role("button", name="New toolset")).to_have_count(0)
        expect(page.get_by_test_id("nv-plat-count")).to_contain_text(" of ")  # "0 of N toolsets": it says how many a filter hides

        empty.get_by_role("button", name="Clear filter").click()

        expect(page.get_by_test_id("nv-plat-empty")).to_have_count(0)
        expect(page.get_by_test_id("nv-plat-filter")).to_have_value("")
        expect(page.get_by_test_id("nv-plat-count")).not_to_contain_text(" of ")
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/toolsets/{toolset_id}")


def _phone(page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})


def test_the_phone_list_says_empty_not_no_matches_when_there_is_no_filter(page, base_url: str, console_url: str) -> None:
    """The lead's screenshot: the Triggers list on a phone said "No matches." with nothing typed in the filter."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        existing = c.get("/v1/triggers", params={"limit": 1}).json()
    if existing.get("items"):
        import pytest

        pytest.skip("this instance already has triggers, so the list is not empty")
    _phone(page)
    open_mobile_platform_nav(page, console_url, "triggers")

    empty = page.get_by_test_id("nv-mob-plat-empty:triggers")
    expect(empty).to_have_text("No triggers yet.", timeout=15_000)

    page.get_by_test_id("nv-mob-plat-filter").fill("zzz")
    expect(empty).to_have_text("No triggers yet.")


def test_the_phone_list_says_nothing_matches_a_filter_that_hides_every_row(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    toolset_id = f"ts-m3m-{unique_suffix}"
    body = {"id": toolset_id, "provider": "mcp", "config": {"transport": "stdio", "config": {"command": ["echo"]}}}
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.post("/v1/toolsets", json=body).status_code == 201
        _phone(page)
        open_mobile_platform_nav(page, console_url, "toolsets")
        expect(page.get_by_test_id(f"nv-mob-plat-row:{toolset_id}")).to_be_visible(timeout=15_000)

        page.get_by_test_id("nv-mob-plat-filter").fill("zzz-no-such-toolset")

        expect(page.get_by_test_id("nv-mob-plat-empty:toolsets")).to_have_text('No toolsets match "zzz-no-such-toolset".', timeout=10_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/toolsets/{toolset_id}")
