"""Platform > Channels: "New channel" opens the dialog on the page, and with no channel provider it offers to add one (ADM-06 and ADM-07 of the 2026-10-08 admin review).

ADM-06: the press used to open the LEGACY Channels list overlay (a table with its own filter and "New channel") and only a second press opened the dialog.
ADM-07: with no channel provider that overlay's "New channel" was disabled and said "Create a channel provider first." as prose with no way to follow it.
These journeys drive the real Platform page: with no provider the dialog explains and its action goes to Platform > Providers; with a
provider the dialog is the page's own, no management overlay is mounted, Cancel leaves the grid and the address alone, and a created channel appears in the grid.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")

_FAKE_DISCORD_TOKEN = "x" * 60


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


def _channel_providers(base_url: str) -> list[dict]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return c.get("/v1/channel_providers", params={"limit": 200}).json().get("items", [])


def test_new_channel_with_no_provider_offers_to_add_one_instead_of_a_dead_end(page, base_url: str, console_url: str) -> None:
    if _channel_providers(base_url):
        pytest.skip("this instance already has a channel provider")
    _open_platform_page(page, console_url, "channels")

    page.get_by_test_id("nv-plat-create").click()

    dialog = page.locator(".modal-overlay")
    expect(dialog).to_contain_text("channel provider", timeout=10_000)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    # An action, not prose: it goes to Platform > Providers, where channel providers are added.
    dialog.get_by_role("button", name="Add a channel provider").click()

    expect(page.get_by_test_id("nv-plat-page:providers")).to_be_visible(timeout=15_000)
    expect(page.locator(".modal-overlay")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)


def test_new_channel_with_a_provider_opens_the_dialog_on_the_page_and_a_created_channel_lands_in_the_grid(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"cp-adm06-{unique_suffix}"
    channel_id = f"ch-adm06-{unique_suffix}"
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post("/v1/channel_providers", json={"id": provider_id, "provider": "discord", "config": {"bot_token": _FAKE_DISCORD_TOKEN}})
            assert r.status_code == 201, r.text
        _open_platform_page(page, console_url, "channels")
        before = page.url

        page.get_by_test_id("nv-plat-create").click()

        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text("New channel", timeout=10_000)
        expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
        assert "overlay=" not in page.url, page.url

        dialog.get_by_role("button", name="Cancel").click()
        expect(page.locator(".modal-overlay")).to_have_count(0)
        assert page.url == before, (before, page.url)

        page.get_by_test_id("nv-plat-create").click()
        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text("New channel", timeout=10_000)
        dialog.get_by_placeholder("auto-generated").fill(channel_id)
        dialog.get_by_placeholder("C0123ABC456 / chat-id / snowflake").fill("123456789012345678")
        dialog.get_by_role("button", name="Create").click()

        expect(page.locator(".modal-overlay")).to_have_count(0, timeout=15_000)
        # The channels overlay has no per-channel detail, so nothing opens; the card is in the grid behind.
        expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
        page.get_by_test_id("nv-plat-filter").fill(channel_id)
        expect(page.get_by_test_id(f"nv-pcard-del:{channel_id}")).to_be_attached(timeout=15_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/channels/{channel_id}")
            c.delete(f"/v1/channel_providers/{provider_id}")
